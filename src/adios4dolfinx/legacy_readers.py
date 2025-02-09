# Copyright (C) 2023 Jørgen Schartum Dokken
#
# This file is part of adios4dolfinx
#
# SPDX-License-Identifier:    MIT

import pathlib

import adios2
import basix
import dolfinx
import numpy as np
import numpy.typing as npt
import ufl
from mpi4py import MPI

from .adios2_helpers import read_array
from .comm_helpers import send_dofs_and_recv_values
from .utils import compute_dofmap_pos, compute_local_range, find_first, index_owner

__all__ = [
    "read_mesh_from_legacy_checkpoint",
    "read_mesh_from_legacy_h5",
    "read_function_from_legacy_h5",
]


def read_dofmap_legacy(
    comm: MPI.Intracomm,
    filename: pathlib.Path,
    dofmap: str,
    dofmap_offsets: str,
    num_cells_global: np.int64,
    engine: str,
    cells: npt.NDArray[np.int64],
    dof_pos: npt.NDArray[np.int32],
) -> npt.NDArray[np.int64]:
    """
    Read dofmap with given communicator, split in continuous chunks based on number of
    cells in the mesh (global).

    Args:
        comm: MPI communicator
        filename: Path to input file
        dofmap: Variable name for dofmap
        num_cells_global: Number of cells in the global mesh
        engine: ADIOS2 engine type
        cells: Cells (global index) that contain a degree of freedom
        dof_pos: Each entry `dof_pos[i]` corresponds to the local position in the
        `input_dofmap.links(cells[i])[dof_pos[i]]`

    Returns:
        The global dof index in the input data for each dof described by the (cells[i], dof_pos[i]) tuples.

    .. note::
        No MPI communication is done during this call
    """
    local_cell_range = compute_local_range(comm, num_cells_global)

    # Open ADIOS engine
    adios = adios2.ADIOS(comm)
    io = adios.DeclareIO("DofmapReader")
    io.SetEngine(engine)
    infile = io.Open(str(filename), adios2.Mode.Read)

    for i in range(infile.Steps()):
        infile.BeginStep()
        if dofmap_offsets in io.AvailableVariables().keys():
            break
        infile.EndStep()

    d_offsets = io.InquireVariable(dofmap_offsets)
    shape = d_offsets.Shape()
    assert len(shape) == 1
    # As the offsets are one longer than the number of cells, we need to read in with an overlap
    d_offsets.SetSelection(
        [[local_cell_range[0]], [local_cell_range[1] + 1 - local_cell_range[0]]]
    )
    in_offsets = np.empty(
        local_cell_range[1] + 1 - local_cell_range[0],
        dtype=d_offsets.Type().strip("_t"),
    )
    infile.Get(d_offsets, in_offsets, adios2.Mode.Sync)
    # Get the relevant part of the dofmap
    if dofmap not in io.AvailableVariables().keys():
        raise KeyError(f"Dof offsets not found at {dofmap}")
    cell_dofs = io.InquireVariable(dofmap)
    cell_dofs.SetSelection([[in_offsets[0]], [in_offsets[-1] - in_offsets[0]]])
    in_dofmap = np.empty(
        in_offsets[-1] - in_offsets[0], dtype=cell_dofs.Type().strip("_t")
    )
    infile.Get(cell_dofs, in_dofmap, adios2.Mode.Sync)

    in_dofmap = in_dofmap.astype(np.int64)

    # Extract dofmap data
    global_dofs = np.zeros_like(cells, dtype=np.int64)
    for i, (cell, pos) in enumerate(zip(cells, dof_pos.astype(np.int64))):
        input_cell_pos = cell - local_cell_range[0]
        read_pos = np.int32(in_offsets[input_cell_pos] + pos - in_offsets[0])
        global_dofs[i] = in_dofmap[read_pos]
        del input_cell_pos, read_pos
    infile.EndStep()
    adios.RemoveIO("DofmapReader")
    return global_dofs


def send_cells_and_receive_dofmap_index(
    filename: pathlib.Path,
    comm: MPI.Intracomm,
    source_ranks: npt.NDArray[np.int32],
    dest_ranks: npt.NDArray[np.int32],
    output_owners: npt.NDArray[np.int32],
    input_cells: npt.NDArray[np.int64],
    dofmap_pos: npt.NDArray[np.int32],
    num_cells_global: np.int64,
    dofmap_path: str,
    xdofmap_path: str,
    engine: str,
) -> npt.NDArray[np.int64]:
    """
    Given a set of positions in input dofmap, give the global input index of this dofmap entry
    in input file.
    """

    # Compute amount of data to send to each process
    out_size = np.zeros(len(dest_ranks), dtype=np.int32)
    for owner in output_owners:
        proc_pos = find_first(owner, dest_ranks)
        out_size[proc_pos] += 1
        del proc_pos
    recv_size = np.zeros(len(source_ranks), dtype=np.int32)
    mesh_to_data_comm = comm.Create_dist_graph_adjacent(
        source_ranks.tolist(), dest_ranks.tolist(), reorder=False
    )
    # Send sizes to create data structures for receiving from NeighAlltoAllv
    mesh_to_data_comm.Neighbor_alltoall(out_size, recv_size)

    # Sort output for sending
    offsets = np.zeros(len(out_size) + 1, dtype=np.intc)
    offsets[1:] = np.cumsum(out_size)
    out_cells = np.zeros(offsets[-1], dtype=np.int64)
    out_pos = np.zeros(offsets[-1], dtype=np.int32)
    count = np.zeros_like(out_size, dtype=np.int32)
    proc_to_dof = np.zeros_like(input_cells, dtype=np.int32)
    for i, owner in enumerate(output_owners):
        # Find relative position of owner in MPI communicator
        # Could be cached from previous run
        proc_pos = find_first(owner, dest_ranks)

        # Fill output data
        out_cells[offsets[proc_pos] + count[proc_pos]] = input_cells[i]
        out_pos[offsets[proc_pos] + count[proc_pos]] = dofmap_pos[i]

        # Compute map from global out position to relative position in proc
        proc_to_dof[offsets[proc_pos] + count[proc_pos]] = i
        count[proc_pos] += 1
        del proc_pos
    del count

    # Prepare data-structures for receiving
    total_incoming = sum(recv_size)
    inc_cells = np.zeros(total_incoming, dtype=np.int64)
    inc_pos = np.zeros(total_incoming, dtype=np.intc)

    # Compute incoming offset
    inc_offsets = np.zeros(len(recv_size) + 1, dtype=np.intc)
    inc_offsets[1:] = np.cumsum(recv_size)

    # Send data
    s_msg = [out_cells, out_size, MPI.INT64_T]
    r_msg = [inc_cells, recv_size, MPI.INT64_T]
    mesh_to_data_comm.Neighbor_alltoallv(s_msg, r_msg)

    s_msg = [out_pos, out_size, MPI.INT32_T]
    r_msg = [inc_pos, recv_size, MPI.INT32_T]
    mesh_to_data_comm.Neighbor_alltoallv(s_msg, r_msg)

    # Read dofmap from file
    input_dofs = read_dofmap_legacy(
        comm,
        filename,
        dofmap_path,
        xdofmap_path,
        num_cells_global,
        engine,
        inc_cells,
        inc_pos,
    )
    # Send input dofs back to owning process
    data_to_mesh_comm = comm.Create_dist_graph_adjacent(
        dest_ranks.tolist(), source_ranks.tolist(), reorder=False
    )

    incoming_global_dofs = np.zeros(sum(out_size), dtype=np.int64)
    s_msg = [input_dofs, recv_size, MPI.INT64_T]
    r_msg = [incoming_global_dofs, out_size, MPI.INT64_T]
    data_to_mesh_comm.Neighbor_alltoallv(s_msg, r_msg)

    # Sort incoming global dofs as they were inputted
    sorted_global_dofs = np.zeros_like(incoming_global_dofs, dtype=np.int64)
    assert len(incoming_global_dofs) == len(input_cells)
    for i in range(len(dest_ranks)):
        for j in range(out_size[i]):
            input_pos = offsets[i] + j
            sorted_global_dofs[proc_to_dof[input_pos]] = incoming_global_dofs[input_pos]
    return sorted_global_dofs


def read_mesh_from_legacy_h5(
    comm: MPI.Intracomm, filename: pathlib.Path, meshname: str
) -> dolfinx.mesh.Mesh:
    """
    Read mesh from `h5`-file generated by legacy DOLFIN `HDF5File.write`.

    Args:
        comm: MPI communicator to distribute mesh over
        filename: Path to `h5` file (with extension)
        meshname: Name of mesh in `h5`-file
    """
    # Create ADIOS2 reader
    adios = adios2.ADIOS(comm)
    io = adios.DeclareIO("Mesh reader")
    io.SetEngine("HDF5")

    # Open ADIOS2 Reader
    infile = io.Open(str(filename), adios2.Mode.Read)
    # Get mesh topology (distributed)
    if f"{meshname}/topology" not in io.AvailableVariables().keys():
        raise KeyError(f"Mesh topology not found at '{meshname}/topology'")
    topology = io.InquireVariable(f"{meshname}/topology")
    shape = topology.Shape()
    local_range = compute_local_range(MPI.COMM_WORLD, shape[0])
    topology.SetSelection(
        [[local_range[0], 0], [local_range[1] - local_range[0], shape[1]]]
    )

    mesh_topology = np.empty(
        (local_range[1] - local_range[0], shape[1]), dtype=np.int64
    )
    infile.Get(topology, mesh_topology, adios2.Mode.Sync)

    # Get mesh cell type
    if f"{meshname}/topology/celltype" not in io.AvailableAttributes().keys():
        raise KeyError(f"Mesh cell type not found at '{meshname}/topology/celltype'")
    celltype = io.InquireAttribute(f"{meshname}/topology/celltype")
    cell_type = celltype.DataString()[0]

    # Get mesh geometry
    if f"{meshname}/coordinates" not in io.AvailableVariables().keys():
        raise KeyError(f"Mesh coordintes not found at '{meshname}/coordinates'")
    geometry = io.InquireVariable(f"{meshname}/coordinates")
    shape = geometry.Shape()
    local_range = compute_local_range(MPI.COMM_WORLD, shape[0])
    geometry.SetSelection(
        [[local_range[0], 0], [local_range[1] - local_range[0], shape[1]]]
    )
    mesh_geometry = np.empty(
        (local_range[1] - local_range[0], shape[1]), dtype=np.float64
    )
    infile.Get(geometry, mesh_geometry, adios2.Mode.Sync)

    assert adios.RemoveIO("Mesh reader")

    # Create DOLFINx mesh
    element = basix.ufl.element(
        basix.ElementFamily.P,
        cell_type,
        1,
        basix.LagrangeVariant.equispaced,
        shape=(mesh_geometry.shape[1],),
        gdim=mesh_geometry.shape[1],
    )
    domain = ufl.Mesh(element)
    return dolfinx.mesh.create_mesh(
        MPI.COMM_WORLD, mesh_topology, mesh_geometry, domain
    )


def read_mesh_from_legacy_checkpoint(
    filename: str, cell_type: str = "tetrahedron"
) -> dolfinx.mesh.Mesh:
    """
    Read mesh from `h5`-file generated by legacy DOLFIN `XDMFFile.write_checkpoint`.
    Needs to get the `cell_type` as input, as legacy DOLFIN does not store the cell-type in the
    `h5`-file

    Args:
        filename: Path to `h5` file (with extension)
        celltype: String describing cell-type
    """
    adios = adios2.ADIOS(MPI.COMM_WORLD)
    io = adios.DeclareIO("Mesh reader")
    io.SetEngine("HDF5")

    # Open ADIOS2 Reader
    infile = io.Open(filename, adios2.Mode.Read)
    path = "func/func_0"
    # Get mesh topology (distributed)
    if f"{path}/topology" not in io.AvailableVariables().keys():
        raise KeyError(f"Mesh topology not found at '{path}topology'")
    topology = io.InquireVariable(f"{path}/topology")
    shape = topology.Shape()
    local_range = compute_local_range(MPI.COMM_WORLD, shape[0])
    topology.SetSelection(
        [[local_range[0], 0], [local_range[1] - local_range[0], shape[1]]]
    )

    mesh_topology = np.empty(
        (local_range[1] - local_range[0], shape[1]), dtype=np.int32
    )
    infile.Get(topology, mesh_topology, adios2.Mode.Sync)

    # Get mesh geometry
    if f"{path}/geometry" not in io.AvailableVariables().keys():
        raise KeyError(f"Mesh geometry not found at '{path}/geometry'")
    geometry = io.InquireVariable(f"{path}/geometry")
    shape = geometry.Shape()
    local_range = compute_local_range(MPI.COMM_WORLD, shape[0])
    geometry.SetSelection(
        [[local_range[0], 0], [local_range[1] - local_range[0], shape[1]]]
    )
    mesh_geometry = np.empty(
        (local_range[1] - local_range[0], shape[1]), dtype=np.float64
    )
    infile.Get(geometry, mesh_geometry, adios2.Mode.Sync)

    assert adios.RemoveIO("Mesh reader")

    # Create DOLFINx mesh
    element = basix.ufl.element(
        basix.ElementFamily.P,
        cell_type,
        1,
        basix.LagrangeVariant.equispaced,
        shape=(mesh_geometry.shape[1],),
        gdim=mesh_geometry.shape[1],
    )
    domain = ufl.Mesh(element)

    return dolfinx.mesh.create_mesh(
        MPI.COMM_WORLD, mesh_topology, mesh_geometry, domain
    )


def read_function_from_legacy_h5(
    comm: MPI.Intracomm, filename: pathlib.Path, u: dolfinx.fem.Function
):
    V = u.function_space
    mesh = u.function_space.mesh
    if u.function_space.element.needs_dof_transformations:
        raise RuntimeError(
            "Function-spaces requiring dof permutations are not compatible with legacy data"
        )
    # ----------------------Step 1---------------------------------
    # Compute index of input cells, and position in input dofmap
    local_cells, dof_pos = compute_dofmap_pos(u.function_space)
    input_cells = mesh.topology.original_cell_index[local_cells]

    # Compute mesh->input communicator
    # 1.1 Compute mesh->input communicator
    num_cells_global = mesh.topology.index_map(mesh.topology.dim).size_global
    owners = index_owner(mesh.comm, input_cells, num_cells_global)
    unique_owners = np.unique(owners)
    # FIXME: In C++ use NBX to find neighbourhood
    _tmp_comm = mesh.comm.Create_dist_graph(
        [mesh.comm.rank], [len(unique_owners)], unique_owners, reorder=False
    )
    source, dest, _ = _tmp_comm.Get_dist_neighbors()

    # ----------------------Step 2--------------------------------
    # Get global dofmap indices from input process
    num_cells_global = mesh.topology.index_map(mesh.topology.dim).size_global
    dofmap_indices = send_cells_and_receive_dofmap_index(
        filename,
        comm,
        np.asarray(source, dtype=np.int32),
        np.asarray(dest, dtype=np.int32),
        owners,
        input_cells,
        dof_pos,
        num_cells_global,
        "/mesh/cell_dofs",
        "/mesh/x_cell_dofs",
        "HDF5",
    )

    # ----------------------Step 3---------------------------------
    # Compute owner of global dof on distributed mesh
    num_dof_global = V.dofmap.index_map_bs * V.dofmap.index_map.size_global
    dof_owner = index_owner(mesh.comm, dofmap_indices, num_dof_global)
    # Create MPI neigh comm to owner.
    # NOTE: USE NBX in C++

    # Read input data
    local_array, starting_pos = read_array(filename, "/mesh/vector_0", "HDF5", comm)

    unique_dof_owners = np.unique(dof_owner)
    mesh_to_dof_comm = mesh.comm.Create_dist_graph(
        [mesh.comm.rank], [len(unique_dof_owners)], unique_dof_owners, reorder=False
    )
    dof_source, dof_dest, _ = mesh_to_dof_comm.Get_dist_neighbors()

    # Send global dof indices to correct input process, and receive value of given dof
    local_values = send_dofs_and_recv_values(
        dofmap_indices, dof_owner, comm, local_array, starting_pos
    )

    # ----------------------Step 4---------------------------------
    # Populate local part of array and scatter forward
    u.x.array[: len(local_values)] = local_values
    u.x.scatter_forward()
