# Copyright (c) 2009-2019 The Regents of the University of Michigan
# This file is part of the HOOMD-blue project, released under the BSD 3-Clause
# License.

from hoomd import _hoomd
from hoomd.parameterdicts import TypeParameterDict
from hoomd.parameterdicts import ParameterDict
from hoomd.typeconverter import RequiredArg
from hoomd.typeparam import TypeParameter
from hoomd.hpmc import _hpmc
from hoomd.integrate import _BaseIntegrator
from hoomd.logger import Loggable
import hoomd
import json


# Helper method to inform about implicit depletants citation
# TODO: figure out where to call this
def cite_depletants():
    _citation = hoomd.cite.article(cite_key='glaser2015',
                                   author=['J Glaser',
                                           'A S Karas', 'S C Glotzer'],
                                   title='A parallel algorithm for implicit depletant simulations',
                                   journal='The Journal of Chemical Physics',
                                   volume=143,
                                   pages='184110',
                                   year='2015',
                                   doi='10.1063/1.4935175',
                                   feature='implicit depletants')
    hoomd.cite._ensure_global_bib().add(_citation)


class _HPMCIntegrator(_BaseIntegrator):
    R""" Base class HPMC integrator.

    :py:class:`_HPMCIntegrator` is the base class for all HPMC integrators. It
    provides common interface elements.  Users should not instantiate this class
    directly. Methods documented here are available to all hpmc integrators.

    .. rubric:: State data

    HPMC integrators can save and restore the following state information to gsd
    files:

        * Maximum trial move displacement *d*
        * Maximum trial rotation move *a*
        * Shape parameters for all types.

    State data are *not* written by default. You must explicitly request that
    state data for an mc integrator is written to a gsd file (see
    :py:meth:`hoomd.dump.GSD.dump_state`).

    .. code::

        mc = hoomd.hpmc.shape(...)
        gsd = hoomd.dump.gsd(...)
        gsd.dump_state(mc)

    State data are *not* restored by default. You must explicitly request that
    state data be restored when initializing the integrator.

    .. code::

        init.read_gsd(...)
        mc = hoomd.hpmc.shape(..., restore_state=True)

    See the *State data* section of the `HOOMD GSD schema
    <http://gsd.readthedocs.io/en/latest/schema-hoomd.html>`_ for details on GSD
    data chunk names and how the data are stored.
    """

    _cpp_cls = None

    def __init__(self, seed, d, a, move_ratio, nselect, deterministic):
        super().__init__()

        # Set base parameter dict for hpmc integrators
        param_dict = ParameterDict(seed=int(seed),
                                   move_ratio=float(move_ratio),
                                   nselect=int(nselect),
                                   deterministic=bool(deterministic)
                                   )
        self._param_dict.update(param_dict)

        # Set standard typeparameters for hpmc integrators
        typeparam_d = TypeParameter('d', type_kind='particle_types',
                                    param_dict=TypeParameterDict(float(d),
                                                                 len_keys=1)
                                    )
        typeparam_a = TypeParameter('a', type_kind='particle_types',
                                    param_dict=TypeParameterDict(float(a),
                                                                 len_keys=1)
                                    )

        typeparam_fugacity = TypeParameter('depletant_fugacity',
                                           type_kind='particle_types',
                                           param_dict=TypeParameterDict(
                                               0., len_keys=1)
                                           )

        typeparam_inter_matrix = TypeParameter('interaction_matrix',
                                               type_kind='particle_types',
                                               param_dict=TypeParameterDict(
                                                   True, len_keys=2)
                                               )

        self._extend_typeparam([typeparam_d, typeparam_a,
                                typeparam_fugacity, typeparam_inter_matrix])

    def attach(self, simulation):
        '''initialize the reflected c++ class'''
        sys_def = simulation.state._cpp_sys_def
        if simulation.device.mode == 'GPU':
            self._cpp_cell = _hoomd.CellListGPU(sys_def)
            if simulation._system_communicator is not None:
                self._cpp_cell.setCommunicator(simulation._system_communicator)
            self._cpp_obj = getattr(_hpmc,
                                    self._cpp_cls + 'GPU')(sys_def,
                                                           self._cpp_cell,
                                                           self.seed)
        else:
            self._cpp_obj = getattr(_hpmc,
                                    self._cpp_cls)(sys_def, self.seed)
            self._cpp_cell = None

        super().attach(simulation)

    # Set the external field
    def set_external(self, ext):
        self._cpp_obj.setExternalField(ext.cpp_compute)

    # Set the patch
    def set_PatchEnergyEvaluator(self, patch):
        self._cpp_obj.setPatchEnergy(patch.cpp_evaluator)

    # TODO need to validate somewhere that quaternions are normalized

    @property
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Since this behaves differently for different types of shapes, the
        default behavior just raises an exception. Subclasses can override this
        to properly return.
        """
        raise NotImplementedError(
            "You are using a shape type that is not implemented! "
            "If you want it, please modify the "
            "hoomd.hpmc.integrate._HPMCIntegrator.get_type_shapes function.")

    def _return_type_shapes(self):
        if not self.is_attached:
            return None
        type_shapes = self._cpp_obj.getTypeShapesPy()
        ret = [json.loads(json_string) for json_string in type_shapes]
        return ret

    @Loggable.log(flag='multi')
    def map_overlaps(self):
        R""" Build an overlap map of the system

        Returns:
            List of tuples. True/false value of the i,j entry indicates
            overlap/non-overlap of the ith and jth particles (by tag)

        Note:
            :py:meth:`map_overlaps` does not support MPI parallel simulations.

        Example:
            mc = hpmc.integrate.shape(...)
            mc.shape_param.set(...)
            overlap_map = np.asarray(mc.map_overlaps())
        """

        if not self.is_attached:
            return None
        return self._cpp_obj.mapOverlaps()

    def map_energies(self):
        R""" Build an energy map of the system

        Returns:
            List of tuples. The i,j entry contains the pairwise interaction energy of the ith and jth particles (by tag)

        Note:
            :py:meth:`map_energies` does not support MPI parallel simulations.

        Example:
            mc = hpmc.integrate.shape(...)
            mc.shape_param.set(...)
            energy_map = np.asarray(mc.map_energies())
        """

        # TODO: update map_energies to new API

        self.update_forces()
        N = hoomd.context.current.system_definition.getParticleData().getMaximumTag() + 1
        energy_map = self.cpp_integrator.mapEnergies()
        return list(zip(*[iter(energy_map)] * N))

    @Loggable.log
    def overlaps(self):
        R""" Count the number of overlaps.

        Returns:
            The number of overlaps in the current system configuration

        Example::

            mc = hpmc.integrate.Shape(..)
            mc.shape['A'] = dict(....)
            run(100)
            num_overlaps = mc.overlaps
        """
        if not self.is_attached:
            return None
        self._cpp_obj.communicate(True)
        return self._cpp_obj.countOverlaps(False)

    def test_overlap(self, type_i, type_j, rij, qi, qj, use_images=True,
                     exclude_self=False):
        R""" Test overlap between two particles.

        Args:
            type_i (str): Type of first particle
            type_j (str): Type of second particle
            rij (tuple): Separation vector **rj**-**ri** between the particle
              centers
            qi (tuple): Orientation quaternion of first particle
            qj (tuple): Orientation quaternion of second particle
            use_images (bool): If True, check for overlap between the periodic
              images of the particles by adding
              the image vector to the separation vector
            exclude_self (bool): If both **use_images** and **exclude_self** are
              true, exclude the primary image

        For two-dimensional shapes, pass the third dimension of **rij** as zero.

        Returns:
            True if the particles overlap.
        """
        self.update_forces()

        ti = hoomd.context.current.system_definition.getParticleData().getTypeByName(type_i)
        tj = hoomd.context.current.system_definition.getParticleData().getTypeByName(type_j)

        rij = hoomd.util.listify(rij)
        qi = hoomd.util.listify(qi)
        qj = hoomd.util.listify(qj)
        return self._cpp_obj.py_test_overlap(ti, tj, rij, qi, qj, use_images, exclude_self)

    @Loggable.log(flag='multi')
    def translate_moves(self):
        R""" Get the number of accepted and rejected translate moves.

        Returns:
            The number of accepted and rejected translate moves during the last
            :py:func:`hoomd.run()`.

        Example::

            mc = hpmc.integrate.Shape(..)
            mc.shape['A'] = dict(....)
            run(100)
            t_accept = mc.translate_acceptance

        """
        return self._cpp_obj.getCounters(1).translate

    @Loggable.log(flag='multi')
    def rotate_moves(self):
        R""" Get the number of accepted and reject rotation moves

        Returns:
            The number of accepted and rejected rotate moves during the last
            :py:func:`hoomd.run()`.

        Example::

            mc = hpmc.integrate.shape(..);
            mc.shape_param.set(....);
            run(100)
            t_accept = mc.get_rotate_acceptance();

        """
        return self._cpp_obj.getCounters(1).rotate

    @Loggable.log
    def mps(self):
        R""" Get the number of trial moves per second.

        Returns:
            The number of trial moves per second performed during the last
              :py:func:`hoomd.run()`.

        """
        return self._cpp_obj.getMPS()

    @property
    def counters(self):
        R""" Get all trial move counters.

        Returns:
            counter object which has ``translate``, ``rotate``,
            ``ovelap_checks``, and ``overlap_errors`` attributes. The attributes
            ``translate`` and ``rotate`` are tuples of the accepted and rejected
            respective trial move while ``overlap_checks`` and
            ``overlap_errors`` are integers.
        """
        return self._cpp_obj.getCounters(1)


class Sphere(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for spheres.

    Sphere parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *diameter* (float, **required**) - diameter of the sphere
              (distance units)
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *orientable* (**default: False**) - set to True for spheres with
              orientation

    d (particle type, float): the size of displacement trial moves

    a (particle type, float): the size of rotation trial moves

    fugacity (particle type, float): depletant fugacity (in units of density,
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to
    include overlaps between type 1 and type 2

    seed (int): random number seed

    move_ratio (float): ratio of translation moves to rotation moves

    nselect (int): number of trial moves to perform in each cell

    deterministic (bool): make HPMC integration deterministic on the GPU

    Examples::

        mc = hoomd.hpmc.integrate.Sphere(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(diameter=1.0)
        mc.shape["B"] = dict(diameter=2.0)
        mc.shape["C"] = dict(diameter=1.0, orientable=True)
        print('diameter = ', mc.shape["A"]["diameter"])

    Depletants Example::

        mc = hoomd.hpmc.integrate.Sphere(seed=415236, d=0.3, a=0.4, nselect=8)
        mc.shape["A"] = dict(diameter=1.0)
        mc.shape["B"] = dict(diameter=1.0)
        mc.depletant_fugacity["B"] = 3.0
    """
    _cpp_cls = 'IntegratorHPMCMonoSphere'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            diameter=float,
                                            ignore_statistics=False,
                                            orientable=False,
                                            len_keys=1)
                                        )
        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Examples:
            The types will be 'Sphere' regardless of dimensionality.

            >>> mc.type_shapes
            [{'type': 'Sphere', 'diameter': 1},
             {'type': 'Sphere', 'diameter': 2}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super()._return_type_shapes()


class ConvexPolygon(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for convex polygons (2D).

    ConvexPolygon parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polygon as a
              list of (x, y) tuples
                * Vertices **MUST** be specified in a *counter-clockwise* order.
                * The origin **MUST** be contained within the vertices.
                * Points inside the polygon **MUST NOT** be included.
                * The origin centered circle that encloses all vertices should
                  be of minimal size for optimal performance (e.g. don't put the
                  origin right next to an edge).
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polygon (distance units). Set a non-zero
              sweep_radius to create a spheropolygon

    d (particle type, float): the size of displacement trial moves

    a (particle type, float): the size of rotation trial moves

    fugacity (particle type, float): depletant fugacity (in units of density,
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to
    include overlaps between type 1 and type 2

    seed (int): random number seed

    move_ratio (float): ratio of translation moves to rotation moves

    nselect (int): number of trial moves to perform in each cell

    deterministic (bool): make HPMC integration deterministic on the GPU

    Note:
        For concave polygons, use :py:class:`SimplePolygon`.

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Examples::

        mc = hoomd.hpmc.integrate.ConvexPolygon(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(vertices=[(-0.5, -0.5),
                                       (0.5, -0.5),
                                       (0.5, 0.5),
                                       (-0.5, 0.5)]);
        print('vertices = ', mc.shape["A"]["vertices"])

    """
    _cpp_cls = 'IntegratorHPMCMonoConvexPolygon'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float)],
                                            ignore_statistics=False,
                                            sweep_radius=0.0,
                                            len_keys=1,)
                                        )

        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'Polygon', 'sweep_radius': 0,
              'vertices': [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(ConvexPolygon, self)._return_type_shapes()


class ConvexSpheropolygon(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for convex spheropolygons 
    (2D).

    ConvexSpheropolygon parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polygon as a 
              list of (x, y) tuples
                * Vertices **MUST** be specified in a *counter-clockwise* order.
                * The origin **MUST** be contained within the vertices.
                * Points inside the polygon **MUST NOT** be included.
                * The origin centered circle that encloses all vertices should
                  be of minimal size for optimal performance (e.g. don't put the
                  origin right next to an edge).
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polygon (distance units).

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Useful cases:

     * A 1-vertex spheropolygon is a disk.
     * A 2-vertex spheropolygon is a spherocylinder.

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Examples::

        mc = hoomd.hpmc.integrate.ConvexSpheropolygon(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(vertices=[(-0.5, -0.5),
                                       (0.5, -0.5),
                                       (0.5, 0.5),
                                       (-0.5, 0.5)],
                             sweep_radius=0.1);

        mc.shape["A"] = dict(vertices=[(0,0)],
                             sweep_radius=0.5,
                             ignore_statistics=True);

        print('vertices = ', mc.shape["A"]["vertices"])

    """

    _cpp_cls = 'IntegratorHPMCMonoSpheropolygon'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float)],
                                            sweep_radius=0.0,
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )

        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'Polygon', 'sweep_radius': 0.1,
              'vertices': [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(ConvexSpheropolygon, self)._return_type_shapes()


class SimplePolygon(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for simple polygons (2D).

    Note:
        For simple polygons that are not concave, use :py:class:`ConvexPolygon`,
        it will execute much faster than :py:class:`SimplePolygon`.

    SimplePolygon parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polygon as a 
              list of (x, y) tuples
                * Vertices **MUST** be specified in a *counter-clockwise* order.
                * The polygon may be concave, but edges must not cross.
                * The origin doesn't necessarily need to be inside the shape.
                * The origin centered circle that encloses all vertices should 
                  be of minimal size for optimal performance.
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polygon (distance units). Set a non-zero
              sweep_radius to create a spheropolygon

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Examples::

        mc = hpmc.integrate.SimplePolygon(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(vertices=[(0, 0.5),
                                       (-0.5, -0.5),
                                       (0, 0),
                                       (0.5, -0.5)]);
        print('vertices = ', mc.shape["A"]["vertices"])

    """

    _cpp_cls = 'IntegratorHPMCMonoSimplePolygon'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float)],
                                            ignore_statistics=False,
                                            sweep_radius=0,
                                            len_keys=1)
                                        )

        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'Polygon', 'sweep_radius': 0,
              'vertices': [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(SimplePolygon, self)._return_type_shapes()


class Polyhedron(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for general polyhedra 
    (3D).

    This shape uses an internal OBB tree for fast collision queries.
    Depending on the number of constituent spheres in the tree, different
    values of the number of spheres per leaf node may yield different
    optimal performance. The capacity of leaf nodes is configurable.

    Only triangle meshes and spheres are supported. The mesh must be free of
    self-intersections.

    Polyhedron parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polyhedron as a
              list of (x, y, z) tuples (distance units)
                * The origin **MUST** strictly be contained in the generally
                  nonconvex volume defined by the vertices and faces
                * The (0,0,0) centered sphere that encloses all vertices should
                  be of minimal size for optimal performance (e.g. don't
                  translate the shape such that (0,0,0) right next to a face).
            * *faces* (list, **required**) - list of vertex indices for every
              face
                * For visualization purposes, the faces **MUST** be defined with
                  a counterclockwise winding order to produce an outward normal.
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polyhedron (distance units). Set a
              non-zero sweep_radius to create a spheropolyhedron
            * *overlap* (**default: None**) - only check overlap between
              constituent particles for which *overlap [i] & overlap[j]* is !=0,
              where '&' is the bitwise AND operator.
            * *capacity* (**default: 4**) - set to the maximum number of
              particles per leaf node for better performance
            * *origin* (**default: (0,0,0)**) - a point strictly inside the
              shape, needed for correctness of overlap checks
            * *hull_only* (**default: True**) - if True, only consider
              intersections between hull polygons

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Example::

        mc = hpmc.integrate.Polyhedron(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(vertices=[(-0.5, -0.5, -0.5),
                                       (-0.5, -0.5, 0.5),
                                       (-0.5, 0.5, -0.5),
                                       (-0.5, 0.5, 0.5),
                                       (0.5, -0.5, -0.5),
                                       (0.5, -0.5, 0.5),
                                       (0.5, 0.5, -0.5),
                                       (0.5, 0.5, 0.5)],
                            faces=[[0, 2, 6],
                                   [6, 4, 0],
                                   [5, 0, 4],
                                   [5, 1, 0],
                                   [5, 4, 6],
                                   [5, 6, 7],
                                   [3, 2, 0],
                                   [3, 0, 1],
                                   [3, 6, 2],
                                   [3, 7, 6],
                                   [3, 1, 5],
                                   [3, 5, 7]])
        print('vertices = ', mc.shape["A"]["vertices"])
        print('faces = ', mc.shape["A"]["faces"])

    Depletants Example::

        mc = hpmc.integrate.Polyhedron(seed=415236, d=0.3, a=0.4, nselect=1)
        cube_verts = [(-0.5, -0.5, -0.5),
                      (-0.5, -0.5, 0.5),
                      (-0.5, 0.5, -0.5),
                      (-0.5, 0.5, 0.5),
                      (0.5, -0.5, -0.5),
                      (0.5, -0.5, 0.5),
                      (0.5, 0.5, -0.5),
                      (0.5, 0.5, 0.5)];
        cube_faces = [[0, 2, 6],
                      [6, 4, 0],
                      [5, 0, 4],
                      [5,1,0],
                      [5,4,6],
                      [5,6,7],
                      [3,2,0],
                      [3,0,1],
                      [3,6,2],
                      [3,7,6],
                      [3,1,5],
                      [3,5,7]]
        tetra_verts = [(0.5, 0.5, 0.5),
                       (0.5, -0.5, -0.5),
                       (-0.5, 0.5, -0.5),
                       (-0.5, -0.5, 0.5)];
        tetra_faces = [[0, 1, 2], [3, 0, 2], [3, 2, 1], [3,1,0]];

        mc.shape["A"] = dict(vertices=cube_verts, faces=cube_faces);
        mc.shape["B"] = dict(vertices=tetra_verts,
                             faces=tetra_faces,
                             origin = (0,0,0));
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoPolyhedron'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float, float)],
                                            faces=[[int, int, int]],
                                            sweep_radius=0.0,
                                            capacity=4,
                                            origin=(0., 0., 0.),
                                            hull_only=True,
                                            overlap=[bool],
                                            ignore_statistics=False,
                                            len_keys=1,
                                            explicit_defaults={'overlap': None})
                                        )

        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'Mesh', 'vertices': [[0.5, 0.5, 0.5], [0.5, -0.5, -0.5],
              [-0.5, 0.5, -0.5], [-0.5, -0.5, 0.5]],
              'faces': [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(Polyhedron, self)._return_type_shapes()


class ConvexPolyhedron(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for convex polyhedra (3D).

    ConvexPolyhedron parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polygon as a 
              list of (x, y) tuples
                * The origin **MUST** be contained within the vertices.
                * The origin centered circle that encloses all vertices should
                  be of minimal size for optimal performance (e.g. don't put the
                  origin right next to a face).
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polyhedron (distance units). Set a
              non-zero sweep_radius to create a spheropolyhedron

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Example::

        mc = hpmc.integrate.ConvexPolyhedron(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(vertices=[(0.5, 0.5, 0.5),
                                       (0.5, -0.5, -0.5),
                                       (-0.5, 0.5, -0.5),
                                       (-0.5, -0.5, 0.5)]);
        print('vertices = ', mc.shape["A"]["vertices"])

    Depletants Example::

        mc = hpmc.integrate.ConvexPolyhedron(seed=415236,
                                             d=0.3,
                                             a=0.4,
                                             nselect=1)
        mc.shape["A"] = dict(vertices=[(0.5, 0.5, 0.5),
                                       (0.5, -0.5, -0.5),
                                       (-0.5, 0.5, -0.5),
                                       (-0.5, -0.5, 0.5)]);
        mc.shape["B"] = dict(vertices=[(0.05, 0.05, 0.05),
                                       (0.05, -0.05, -0.05),
                                       (-0.05, 0.05, -0.05),
                                       (-0.05, -0.05, 0.05)]);
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoConvexPolyhedron'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float, float)],
                                            sweep_radius=0.0,
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )
        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'ConvexPolyhedron', 'sweep_radius': 0,
              'vertices': [[0.5, 0.5, 0.5], [0.5, -0.5, -0.5],
                           [-0.5, 0.5, -0.5], [-0.5, -0.5, 0.5]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(ConvexPolyhedron, self)._return_type_shapes()


class FacetedEllipsoid(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for faceted ellipsoids
    (3D).

    A faceted ellipsoid is an ellipsoid intersected with a convex polyhedron
    defined through halfspaces. The equation defining each halfspace is given
    by:

    .. math::
        n_i\cdot r + b_i \le 0

    where :math:`n_i` is the face normal, and :math:`b_i` is  the offset.

    Warning:
        The origin must be chosen so as to lie **inside the shape**, or the
        overlap check will not work. This condition is not checked.

    FacetedEllipsoid parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *normals* (**required**) - list of (x,y,z) tuples defining the
              facet normals (distance units)
            * *offsets* (**required**) - list of offsets (distance unit^2)
            * *a* (**required**) - first half axis of ellipsoid (distance units)
            * *b* (**required**) - second half axis of ellipsoid
              (distance units)
            * *c* (**required**) - third half axis of ellipsoid (distance units)
            * *vertices* (**required**) - list of vertices for intersection
              polyhedron
            * *origin* (**required**) - (x, y, z) tuple representing origin
              vector
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Warning:
        Planes must not be coplanar.

    Note:
        The half-space intersection of the normals has to match the convex
        polyhedron defined by the vertices (if non-empty), currently the
        half-space intersection is **not** calculated automatically. For
        simple intersections with planes that do not intersect within the
        sphere, the vertices list can be left empty.

    Example::

        mc = hpmc.integrate.FacetedEllipsoid(seed=415236, d=0.3, a=0.4)

        # half-space intersection
        slab_normals = [(-1,0,0),(1,0,0),(0,-1,0),(0,1,0),(0,0,-1),(0,0,1)]
        slab_offsets = [-0.1,-1,-.5,-.5,-.5,-.5]

        # polyedron vertices
        slab_verts = [[-.1,-.5,-.5],
                      [-.1,-.5,.5],
                      [-.1,.5,.5],
                      [-.1,.5,-.5],
                      [1,-.5,-.5],
                      [1,-.5,.5],
                      [1,.5,.5],
                      [1,.5,-.5]]

        mc.shape["A"] = dict(normals=slab_normals,
                             offsets=slab_offsets,
                             vertices=slab_verts,
                             a=1.0,
                             b=0.5,
                             c=0.5);
        print('a = {}, b = {}, c = {}',
              mc.shape["A"]["a"], mc.shape["A"]["b"], mc.shape["A"]["c"])

    Depletants Example::

        mc = hpmc.integrate.FacetedEllipsoid(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(normals=[(-1,0,0),
                                      (1,0,0),
                                      (0,-1,0),
                                      (0,1,0),
                                      (0,0,-1),
                                      (0,0,1)],
                             a=1.0,
                             b=0.5,
                             c=0.25);
        # depletant sphere
        mc.shape["B"] = dict(normals=[], a=0.1, b=0.1, c=0.1);
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoFacetedEllipsoid'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            normals=[(float, float, float)],
                                            offsets=[int],
                                            a=float,
                                            b=float,
                                            c=float,
                                            vertices=[(float, float, float)],
                                            origin=tuple,
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )
        self._add_typeparam(typeparam_shape)


class Sphinx(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for sphinx particles (3D).

    Sphinx particles are dimpled spheres (spheres with 'positive' and 'negative'
    volumes).

    Sphinx parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *diameters* - diameters of spheres (positive OR negative real
              numbers)
            * *centers* - centers of spheres in local coordinate frame
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Sphinx particles are dimpled spheres (spheres with 'positive' and 'negative'
    volumes).

    Example::

        mc = hpmc.integrate.Sphinx(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(centers=[(0,0,0),(1,0,0)], diameters=[1,.25])
        print('diameters = ', mc.shape["A"]["diameters"])

    Depletants Example::

        mc = hpmc.integrate.Sphinx(seed=415236, d=0.3, a=0.4, nselect=1)
        mc.shape["A"] = dict(centers=[(0,0,0), (1,0,0)], diameters=[1, -.25])
        mc.shape["B"] = dict(centers=[(0,0,0)], diameters=[.15])
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoSphinx'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            diameters=[float],
                                            centers=[(float, float, float)],
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )
        self._add_typeparam(typeparam_shape)


class ConvexSpheropolyhedron(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for convex spheropolyhedra
    (3D).

    A spheropolyhedron can also represent spheres (0 or 1 vertices), and
    spherocylinders (2 vertices).

    ConvexSpheropolyhedron parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *vertices* (list, **required**) - vertices of the polygon as a 
              list of (x, y) tuples
                * The origin **MUST** be contained within the vertices.
                * The origin centered circle that encloses all vertices should
                  be of minimal size for optimal performance (e.g. don't put the
                  origin right next to a face).
                * A sphere can be represented by specifying zero vertices
                  (i.e. vertices=[]) and a non-zero radius R
                * Two vertices and a non-zero radius R define a prolate
                  spherocylinder.
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics
            * *sweep_radius* (**default: 0.0**) - radius of the sphere swept
              around the edges of the polyhedron (distance units).

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Warning:
        HPMC does not check that all requirements are met. Undefined behavior
        will result if they are violated.

    Example::

        mc = hpmc.integrate.ConvexSpheropolyhedron(seed=415236, d=0.3, a=0.4)
        mc.shape['tetrahedron'] = dict(vertices=[(0.5, 0.5, 0.5),
                                                 (0.5, -0.5, -0.5),
                                                 (-0.5, 0.5, -0.5),
                                                 (-0.5, -0.5, 0.5)]);
        print('vertices = ', mc.shape['tetrahedron']["vertices"])

        mc.shape['SphericalDepletant'] = dict(vertices=[],
                                              sweep_radius=0.1,
                                              ignore_statistics=True);

    Depletants example::

        mc = hpmc.integrate.ConvexSpheropolyhedron(seed=415236, d=0.3, a=0.4)
        mc.shape["tetrahedron"] = dict(vertices=[(0.5, 0.5, 0.5),
                                                 (0.5, -0.5, -0.5),
                                                 (-0.5, 0.5, -0.5),
                                                 (-0.5, -0.5, 0.5)]);
        mc.shape["SphericalDepletant"] = dict(vertices=[], sweep_radius=0.1);
        mc.depletant_fugacity["SphericalDepletant"] = 3.0

    """

    _cpp_cls = 'IntegratorHPMCMonoSpheropolyhedron'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
               nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            vertices=[(float, float, float)],
                                            sweep_radius=0.0,
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )
        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Example:
            >>> mc.type_shapes()
            [{'type': 'ConvexPolyhedron', 'sweep_radius': 0.1,
              'vertices': [[0.5, 0.5, 0.5], [0.5, -0.5, -0.5],
                           [-0.5, 0.5, -0.5], [-0.5, -0.5, 0.5]]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super(ConvexSpheropolyhedron, self)._return_type_shapes()


class Ellipsoid(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for ellipsoids (2D/3D).

    Ellipsoid parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *a* (**required**) - principle axis a of the ellipsoid (radius in
              the x direction) (distance units)
            * *b* (**required**) - principle axis b of the ellipsoid (radius in
              the y direction) (distance units)
            * *c* (**required**) - principle axis c of the ellipsoid (radius in
              the z direction) (distance units)
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Example::

        mc = hpmc.integrate.Ellipsoid(seed=415236, d=0.3, a=0.4)
        mc.shape["A"] = dict(a=0.5, b=0.25, c=0.125);
        print('ellipsoids parameters (a,b,c) = ',
              mc.shape["A"]["a"],
              mc.shape["A"]["b"],
              mc.shape["A"]["c"])

    Depletants Example::

        mc = hpmc.integrate.Ellipsoid(seed=415236, d=0.3, a=0.4, nselect=1)
        mc.shape["A"] = dict(a=0.5, b=0.25, c=0.125);
        mc.shape["B"] = dict(a=0.05, b=0.05, c=0.05);
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoEllipsoid'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter('shape', type_kind='particle_types',
                                        param_dict=TypeParameterDict(
                                            a=float,
                                            b=float,
                                            c=float,
                                            ignore_statistics=False,
                                            len_keys=1)
                                        )

        self._extend_typeparam([typeparam_shape])

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.
        Example:
            >>> mc.type_shapes()
            [{'type': 'Ellipsoid', 'a': 1.0, 'b': 1.5, 'c': 1}]
        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super()._return_type_shapes()


class SphereUnion(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for unions of spheres
    (3D).

    This shape uses an internal OBB tree for fast collision queries.
    Depending on the number of constituent spheres in the tree, different values
    of the number of spheres per leaf node may yield different optimal
    performance. The capacity of leaf nodes is configurable.

    SphereUnion parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *shapes* (**required**) - list of sphere objects to be included in
              union
            * *positions* (**required**) - list of centers of constituent
              spheres in particle coordinates.
            * *orientations* (**required**) - list of orientations of
              constituent spheres.
            * *overlap* (**default: 1**) - only check overlap
              between constituent particles for which *overlap [i] & overlap[j]*
              is !=0, where '&' is the bitwise AND operator.
            * *capacity* (**default: 4**) - set to the maximum number of
              particles per leaf node for better performance
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Example::

        mc = hpmc.integrate.SphereUnion(seed=415236, d=0.3, a=0.4)
        sphere1 = dict(diameter=1)
        sphere2 = dict(diameter=2)
        mc.shape["A"] = dict(shapes=[sphere1, sphere2],
                             positions=[(0, 0, 0), (0, 0, 1)],
                             orientations=[(1, 0, 0, 0), (1, 0, 0, 0)],
                             overlap=[1, 1])
        print('diameter of the first sphere = ',
              mc.shape["A"]["shapes"][0]["diameter"])
        print('center of the first sphere = ', mc.shape["A"]["positions"][0])

    Depletants Example::

        mc = hpmc.integrate.SphereUnion(seed=415236, d=0.3, a=0.4, nselect=1)
        mc.shape["A"] = dict(diameters=[1.0, 1.0],
                             centers=[(-0.25, 0.0, 0.0),
                                      (0.25, 0.0, 0.0)]);
        mc.shape["B"] = dict(diameters=[0.05], centers=[(0.0, 0.0, 0.0)]);
        mc.depletant_fugacity["B"] = 3.0
    """

    _cpp_cls = 'IntegratorHPMCMonoSphereUnion'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter(
            'shape', type_kind='particle_types',
            param_dict=TypeParameterDict(
                shapes=[dict(diameter=float, ignore_statistics=False,
                             orientable=False)],
                positions=[(float, float, float)],
                orientations=[(float, float, float, float)],
                capacity=4,
                overlap=[bool],
                ignore_statistics=False,
                len_keys=1,
                explicit_defaults={'orientations': None,
                                   'overlap': None})
            )
        self._add_typeparam(typeparam_shape)

    @Loggable.log(flag='multi')
    def type_shapes(self):
        """Get all the types of shapes in the current simulation.

        Examples:
            The type will be 'SphereUnion' regardless of dimensionality.

            >>> mc.type_shapes
            [{'type': 'SphereUnion', 'centers': [[0, 0, 0], [0, 0, 1]], 'diameters': [1, 0.5]},
             {'type': 'SphereUnion', 'centers': [[1, 2, 3], [4, 5, 6]], 'diameters': [0.5, 1]}]

        Returns:
            A list of dictionaries, one for each particle type in the system.
        """
        return super()._return_type_shapes()


class ConvexSpheropolyhedronUnion(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for unions of convex
    polyhedra (3D).

    ConvexSpheropolyhedronUnion parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *shapes* (**required**) - list of polyhedron objects to be
              included in union
            * *positions* (**required**) - list of centers of constituent
              polyhedra in particle coordinates.
            * *orientations* (**required**) - list of orientations of
              constituent polyhedra.
            * *overlap* (**default: 1**) - only check overlap
              between constituent particles for which *overlap [i] & overlap[j]*
              is !=0, where '&' is the bitwise AND operator.
            * *sweep_radii* (**default: 0 for all particle**) - radii of spheres
              sweeping out each constituent polyhedron
            * *capacity* (**default: 4**) - set to the maximum number of
              particles per leaf node for better performance
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Example::

        mc = hoomd.hpmc.integrate.ConvexSpheropolyhedronUnion(seed=27,
                                                              d=0.3,
                                                              a=0.4)
        cube_verts = [[-1,-1,-1],
                      [-1,-1,1],
                      [-1,1,1],
                      [-1,1,-1],
                      [1,-1,-1],
                      [1,-1,1],
                      [1,1,1],
                      [1,1,-1]]
        mc.shape["A"] = dict(shapes=[cube_verts, cube_verts],
                             positions=[(0, 0, 0), (0, 0, 1)],
                             orientations=[(1, 0, 0, 0), (1, 0, 0, 0)],
                             overlap=[1, 1]);
        print('vertices of the first cube = ',
              mc.shape["A"]["shapes"][0]["vertices"])
        print('center of the first cube = ', mc.shape["A"]["positions"][0])
        print('orientation of the first cube = ',
              mc.shape_param["A"]["orientations"][0])
    """

    _cpp_cls = 'IntegratorHPMCMonoConvexPolyhedronUnion'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter(
            'shape', type_kind='particle_types',
            param_dict=TypeParameterDict(
                shapes=[dict(vertices=[(float, float, float)],
                             sweep_radius=0.0,
                             ignore_statistics=False)],
                positions=[(float, float, float)],
                orientations=[(float, float, float, float)],
                overlap=[bool],
                ignore_statistics=False,
                capacity=4,
                len_keys=1,
                explicit_defaults={'orientations': None,
                                   'overlap': None})
            )

        self._add_typeparam(typeparam_shape)
        # meta data
        self.metadata_fields = ['capacity']


class FacetedEllipsoidUnion(_HPMCIntegrator):
    R""" Hard particle Monte Carlo integration method for unions of faceted
    ellipsoids (3D).

    See :py:class:`FacetedEllipsoid` for a detailed explanation of the
    constituent particle parameters.

    FacetedEllipsoidUnion parameters:

    shape (particle type, dict): defines the shape of the object.

        Keys:
            * *shapes* (**required**) - list of ellipsoid objects to be included
              in union
            * *positions* (**required**) - list of centers of constituent
              ellipsoids in particle coordinates.
            * *orientations* (**required**) - list of orientations of
              constituent ellipsoids.
            * *overlap* (**default: 1**) - only check overlap
              between constituent particles for which *overlap [i] & overlap[j]*
              is !=0, where '&' is the bitwise AND operator.
            * *capacity* (**default: 4**) - set to the maximum number of
              particles per leaf node for better performance
            * *ignore_statistics* (**default: False**) - set to True to ignore
              tracked statistics

    d (particle type, float): the size of displacement trial moves
        
    a (particle type, float): the size of rotation trial moves
        
    fugacity (particle type, float): depletant fugacity (in units of density, 
    volume^-1)

    interaction_matrix ((particle type, particle type), bool): whether to 
    include overlaps between type 1 and type 2
    
    seed (int): random number seed
    
    move_ratio (float): ratio of translation moves to rotation moves
    
    nselect (int): number of trial moves to perform in each cell
    
    deterministic (bool): make HPMC integration deterministic on the GPU

    Example::

        mc = hpmc.integrate.FacetedEllipsoidUnion(seed=27, d=0.3, a=0.4)

        # make a prolate Janus ellipsoid
        # cut away -x halfspace
        normals = [(-1,0,0)]
        offsets = [0]
        slab_normals = [(-1,0,0),(1,0,0),(0,-1,0),(0,1,0),(0,0,-1),(0,0,1)]
        slab_offsets = [-0.1,-1,-.5,-.5,-.5,-.5)

        # polyedron vertices
        slab_verts = [[-.1,-.5,-.5],
                      [-.1,-.5,.5],
                      [-.1,.5,.5],
                      [-.1,.5,-.5],
                      [1,-.5,-.5],
                      [1,-.5,.5],
                      [1,.5,.5],
                      [1,.5,-.5]]

        faceted_ellipsoid1 = dict(normals=slab_normals,
                                  offsets=slab_offsets,
                                  vertices=slab_verts,
                                  a=1.0,
                                  b=0.5,
                                  c=0.5);
        faceted_ellipsoid2 = dict(normals=slab_normals,
                                  offsets=slab_offsets,
                                  vertices=slab_verts,
                                  a=0.5,
                                  b=1,
                                  c=1);

        mc.shape["A"] = dict(shapes=[faceted_ellipsoid1, faceted_ellipsoid2],
                             positions=[(0, 0, 0), (0, 0, 1)],
                             orientations=[(1, 0, 0, 0), (1, 0, 0, 0)],
                             overlap=[1, 1]);

        print('offsets of the first faceted ellipsoid = ',
              mc.shape["A"]["shapes"][0]["offsets"])
        print('normals of the first faceted ellispoid = ',
              mc.shape["A"]["shapes"][0]["normals"])
        print('vertices of the first faceted ellipsoid = ',
              mc.shape["A"]["shapes"][0]["vertices"]
    """

    _cpp_cls = 'IntegratorHPMCMonoFacetedEllipsoidUnion'

    def __init__(self, seed, d=0.1, a=0.1, move_ratio=0.5,
                 nselect=4, deterministic=False):

        # initialize base class
        super().__init__(seed, d, a, move_ratio, nselect, deterministic)

        typeparam_shape = TypeParameter(
            'shape', type_kind='particle_types',
            param_dict=TypeParameterDict(
                shapes=[
                    dict(a=float, b=float, c=float,
                         normals=[(float, float, float)], offsets=[int],
                         vertices=[(float, float, float)], origin=tuple,
                         ignore_statistics=False)],
                positions=[(float, float, float)],
                orientations=[(float, float, float, float)],
                overlap=[bool],
                ignore_statistics=False,
                capacity=4,
                len_keys=1,
                explicit_defaults={'orientations': None,
                                   'overlap': None})
            )
        self._add_typeparam(typeparam_shape)
