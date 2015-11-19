/*
Highly Optimized Object-oriented Many-particle Dynamics -- Blue Edition
(HOOMD-blue) Open Source Software License Copyright 2009-2015 The Regents of
the University of Michigan All rights reserved.

HOOMD-blue may contain modifications ("Contributions") provided, and to which
copyright is held, by various Contributors who have granted The Regents of the
University of Michigan the right to modify and/or distribute such Contributions.

You may redistribute, use, and create derivate works of HOOMD-blue, in source
and binary forms, provided you abide by the following conditions:

* Redistributions of source code must retain the above copyright notice, this
list of conditions, and the following disclaimer both in the code and
prominently in any materials provided with the distribution.

* Redistributions in binary form must reproduce the above copyright notice, this
list of conditions, and the following disclaimer in the documentation and/or
other materials provided with the distribution.

* All publications and presentations based on HOOMD-blue, including any reports
or published results obtained, in whole or in part, with HOOMD-blue, will
acknowledge its use according to the terms posted at the time of submission on:
http://codeblue.umich.edu/hoomd-blue/citations.html

* Any electronic documents citing HOOMD-Blue will link to the HOOMD-Blue website:
http://codeblue.umich.edu/hoomd-blue/

* Apart from the above required attributions, neither the name of the copyright
holder nor the names of HOOMD-blue's contributors may be used to endorse or
promote products derived from this software without specific prior written
permission.

Disclaimer

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDER AND CONTRIBUTORS ``AS IS'' AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND/OR ANY
WARRANTIES THAT THIS SOFTWARE IS FREE OF INFRINGEMENT ARE DISCLAIMED.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
*/

// Maintainer: jglaser

#include "ForceDistanceConstraintGPU.h"
#include "ForceDistanceConstraintGPU.cuh"

#include <string.h>

#include <boost/python.hpp>
#include <boost/bind.hpp>

/*! \file ForceDistanceConstraintGPU.cc
    \brief Contains code for the ForceDistanceConstraintGPU class
*/

/*! \param sysdef SystemDefinition containing the ParticleData to compute forces on
*/
ForceDistanceConstraintGPU::ForceDistanceConstraintGPU(boost::shared_ptr<SystemDefinition> sysdef)
        : ForceDistanceConstraint(sysdef), m_constraints_dirty(true), m_nnz(m_exec_conf), m_nnz_tot(0),
        m_csr_val(m_exec_conf), m_csr_rowptr(m_exec_conf), m_csr_colind(m_exec_conf)
    {
    m_tuner_fill.reset(new Autotuner(32, 1024, 32, 5, 100000, "dist_constraint_fill_matrix_vec", this->m_exec_conf));
    m_tuner_force.reset(new Autotuner(32, 1024, 32, 5, 100000, "dist_constraint_force", this->m_exec_conf));

    cusparseCreate(&m_cusparse_handle);
    cusparseCreateSolveAnalysisInfo(&m_cusparse_solve_info);

    // cusparse matrix descriptor
    cusparseCreateMatDescr(&m_cusparse_mat_descr);
    cusparseSetMatType(m_cusparse_mat_descr,CUSPARSE_MATRIX_TYPE_GENERAL);
    cusparseSetMatIndexBase(m_cusparse_mat_descr,CUSPARSE_INDEX_BASE_ZERO);
    cusparseSetMatDiagType(m_cusparse_mat_descr, CUSPARSE_DIAG_TYPE_NON_UNIT);

    // connect to the ConstraintData to recieve notifications when constraints change order in memory
    m_constraints_dirty_connection = m_cdata->connectGroupsDirty(boost::bind(&ForceDistanceConstraintGPU::slotConstraintsDirty, this));
    }

//! Destructor
ForceDistanceConstraintGPU::~ForceDistanceConstraintGPU()
    {
    // clean up cusparse
    cusparseDestroy(m_cusparse_handle);
    cusparseDestroySolveAnalysisInfo(m_cusparse_solve_info);
    cusparseDestroyMatDescr(m_cusparse_mat_descr);

    // disconnect from signal in ConstaraintData
    m_constraints_dirty_connection.disconnect();
    }

void ForceDistanceConstraintGPU::fillMatrixVector(unsigned int timestep)
    {
    if (m_prof)
        m_prof->push(m_exec_conf, "fill matrix");

    // fill the matrix in row-major order
    unsigned int n_constraint = m_cdata->getN();

    // access particle data
    ArrayHandle<Scalar4> d_pos(m_pdata->getPositions(), access_location::device, access_mode::read);
    ArrayHandle<Scalar4> d_vel(m_pdata->getVelocities(), access_location::device, access_mode::read);
    ArrayHandle<unsigned int> d_rtag(m_pdata->getRTags(), access_location::device, access_mode::read);
    ArrayHandle<Scalar4> d_netforce(m_pdata->getNetForce(), access_location::device, access_mode::read);

        {
        // access matrix elements
        ArrayHandle<Scalar> d_cmatrix(m_cmatrix, access_location::device, access_mode::overwrite);
        ArrayHandle<Scalar> d_cvec(m_cvec, access_location::device, access_mode::overwrite);

        // access GPU constraint table on device
        const GPUArray<BondData::members_t>& gpu_constraint_list = this->m_cdata->getGPUTable();
        const Index2D& gpu_table_indexer = this->m_cdata->getGPUTableIndexer();

        ArrayHandle<BondData::members_t> d_gpu_clist(gpu_constraint_list, access_location::device, access_mode::read);
        ArrayHandle<unsigned int > d_gpu_n_constraints(this->m_cdata->getNGroupsArray(),
                                                 access_location::device, access_mode::read);
        ArrayHandle<unsigned int> d_gpu_cpos(m_cdata->getGPUPosTable(), access_location::device, access_mode::read);
        ArrayHandle<typeval_t> d_group_typeval(m_cdata->getTypeValArray(), access_location::device, access_mode::read);

        // launch GPU kernel
        m_tuner_fill->begin();
        gpu_fill_matrix_vector(
            n_constraint,
            m_pdata->getN(),
            d_cmatrix.data,
            d_cvec.data,
            d_pos.data,
            d_vel.data,
            d_netforce.data,
            d_gpu_clist.data,
            gpu_table_indexer,
            d_gpu_n_constraints.data,
            d_gpu_cpos.data,
            d_group_typeval.data,
            m_deltaT,
            m_pdata->getBox(),
            m_constraints_dirty,
            m_tuner_fill->getParam());

        if (m_exec_conf->isCUDAErrorCheckingEnabled())
            CHECK_CUDA_ERROR();

        m_tuner_fill->end();
        }

    if (m_prof)
        m_prof->pop(m_exec_conf);
    }

void ForceDistanceConstraintGPU::computeConstraintForces(unsigned int timestep)
    {
    if (m_prof)
        m_prof->push(m_exec_conf,"constraint forces");

    unsigned int n_constraint = m_cdata->getN();

    bool done = false;
    while (!done)
        {
        // reallocate array of constraint forces
        m_lagrange.resize(n_constraint);

        // resize sparse matrix storage
        m_nnz.resize(n_constraint);
        m_csr_rowptr.resize(n_constraint+1);
        m_csr_colind.resize(n_constraint*n_constraint);
        m_csr_val.resize(n_constraint*n_constraint);

            {
            // access matrix and vector
            ArrayHandle<Scalar> d_cmatrix(m_cmatrix, access_location::device, access_mode::read);
            ArrayHandle<Scalar> d_cvec(m_cvec, access_location::device, access_mode::read);
            ArrayHandle<Scalar> d_lagrange(m_lagrange, access_location::device, access_mode::overwrite);

            // access sparse matrix structural data
            ArrayHandle<int> d_nnz(m_nnz, access_location::device, access_mode::readwrite);
            ArrayHandle<int> d_csr_colind(m_csr_colind, access_location::device, access_mode::readwrite);
            ArrayHandle<int> d_csr_rowptr(m_csr_rowptr, access_location::device, access_mode::readwrite);
            ArrayHandle<Scalar> d_csr_val(m_csr_val, access_location::device, access_mode::readwrite);

            // access particle data arrays
            ArrayHandle<Scalar4> d_pos(m_pdata->getPositions(), access_location::device, access_mode::read);

            // access force array
            ArrayHandle<Scalar4> d_force(m_force, access_location::device, access_mode::overwrite);

            // access GPU constraint table on device
            const GPUArray<BondData::members_t>& gpu_constraint_list = this->m_cdata->getGPUTable();
            const Index2D& gpu_table_indexer = this->m_cdata->getGPUTableIndexer();

            ArrayHandle<BondData::members_t> d_gpu_clist(gpu_constraint_list, access_location::device, access_mode::read);
            ArrayHandle<unsigned int > d_gpu_n_constraints(this->m_cdata->getNGroupsArray(),
                                                     access_location::device, access_mode::read);
            ArrayHandle<unsigned int> d_gpu_cpos(m_cdata->getGPUPosTable(), access_location::device, access_mode::read);

            const BoxDim& box = m_pdata->getBox();

            unsigned int n_ptl = m_pdata->getN();

            // compute constraint forces by solving linear system of equations
            m_tuner_force->begin();
            gpu_compute_constraint_forces(n_constraint,
                d_cmatrix.data,
                d_cvec.data,
                d_nnz.data,
                m_nnz_tot,
                d_pos.data,
                d_gpu_clist.data,
                gpu_table_indexer,
                d_gpu_n_constraints.data,
                d_gpu_cpos.data,
                d_force.data,
                box,
                n_ptl,
                m_tuner_force->getParam(),
                m_cusparse_handle,
                m_cusparse_mat_descr,
                m_cusparse_solve_info,
                m_constraints_dirty,
                d_csr_rowptr.data,
                d_csr_colind.data,
                d_csr_val.data,
                d_lagrange.data);

            if (m_exec_conf->isCUDAErrorCheckingEnabled())
                CHECK_CUDA_ERROR();

            m_tuner_force->end();
            }

        // if we have just initialized the solver, re-run
        if (m_constraints_dirty)
            {
            m_constraints_dirty = false;

            // now fill with real values
            fillMatrixVector(timestep);
            }
        else
            {
            done = true;
            }
        }

    if (m_prof)
        m_prof->pop(m_exec_conf);

    }

void export_ForceDistanceConstraintGPU()
    {
    class_< ForceDistanceConstraintGPU, boost::shared_ptr<ForceDistanceConstraintGPU>, bases<ForceConstraint>, boost::noncopyable >
    ("ForceDistanceConstraintGPU", init< boost::shared_ptr<SystemDefinition> >())
    ;
    }
