#
#  Copyright 2019 California Institute of Technology
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# ISOFIT: Imaging Spectrometer Optimal FITting
# Author: Niklas Bohn, urs.n.bohn@jpl.nasa.gov
#         Jouni Susiluoto, jouni.i.susiluoto@jpl.nasa.gov
#

import h5py
import logging
import numpy as np

from isofit.configs.sections.radiative_transfer_config import RadiativeTransferEngineConfig
from isofit.core.common import resample_spectrum, spectral_response_function

Logger = logging.getLogger(__file__)


class KernelFlowsRT(object):
    """
    Radiative transfer emulation based on KernelFlows.jl and VSWIREmulator.jl. A description of
    the model can be found in:

        O. Lamminpää, J. Susiluoto, J. Hobbs, J. McDuffie, A. Braverman, and H. Owhadi.
        Forward model emulator for atmospheric radiative transfer using Gaussian processes
        and cross validation (2024). Submitted to Atmospheric Measurement Techniques.
    """

    def __init__(
            self,
            engine_config: RadiativeTransferEngineConfig,
            wl,
            fwhm
    ):
        # read VSWIREmulator struct from jld2 file
        self.input_transfs = [np.identity, np.log, np.cos]
        self.f = h5py.File(engine_config.emulator_file, "r")
        # ga components of size (npar1, npar2, ...nparn, nbands). only ouputs necessary.
        self.wl = wl
        self.fwhm = fwhm
        self.srf_matrix = np.array(
            [
                spectral_response_function(np.array(self.f["wls"]), wi, fwhmi / 2.355)
                for wi, fwhmi in zip(self.wl, self.fwhm)
            ]
        )

        # load solar irradiance
        # ToDo: remove hardcoded path
        irr_file = test = "/Users/bohn/Desktop/JPL_ISOFIT_Project/isofit_build_workflow/data/kurucz_0.1nm.dat"
        irr_wl, irr = np.loadtxt(irr_file, comments="#").T
        irr = irr / 10  # convert to uW cm-2 sr-1 nm-1
        irr_resamp = resample_spectrum(irr, irr_wl, self.wl, self.fwhm)
        self.irr_resamp = np.array(irr_resamp, dtype=np.float32)

        self.ga = list()

    def predict(self, points):
        nMVMs = len(self.f.keys()) - 6
        for i in range(1, 1+nMVMs):
            MVM = self.predict_single_MVM(self.f["MVM" + str(i)], points,
                                          self.f["input_transfs"][i-1, :])
            self.ga.append(MVM)

        combined = {
            "rhoatm": self.ga[0],
            "sphalb": self.ga[3],
            "transm_down_dir": self.ga[1],
            "transm_down_dif": self.ga[2],
            "transm_up_dir": np.zeros(self.ga[0].shape),
            "transm_up_dif": np.zeros(self.ga[0].shape),
            "thermal_upwelling": np.zeros(self.ga[0].shape),
            "thermal_downwelling": np.zeros(self.ga[0].shape),
            "solar_irr": self.irr_resamp,
            "wl": self.wl
        }
        return combined

    def predict_single_MVM(self, MVM, points, transfs):
        points = np.copy(points)  # don't overwrite inputs
        for i, j in enumerate(transfs):
            if j == 1:
                continue
            points[:, i] = self.input_transfs[j-1](points[:, i])

        nte = np.shape(points)[0]
        nzycols = len(MVM.keys()) - 1  # take out GPGeometry from list of GPs
        ZY_pred = np.zeros((nte, nzycols))

        for i in range(nzycols):
            M = MVM["M" + str(i+1)]
            G = MVM["G"]  # shorthand
            Z = self.reduce_points(points, G["Xproj" + str(i+1)], G["Xmean"], G["Xstd"])
            ZY_pred[:, i] = self.predict_M(M, Z)

        # H is same as in recover() in dimension_reduction.jl
        MP = self.srf_matrix @ G["Yproj"]["vectors"][:, :].T
        H = MP * G["Yproj"]["values"][:]
        srfmean = self.srf_matrix @ G["Ymean"]
        return ZY_pred @ H.T + srfmean

    def predict_M(self, M, Z_te):
        Z_tr = M["Z"][:, :].T  # training inputs
        Z_te = Z_te * M["lambda"][:]  # scale test inputs
        theta = M["theta"][:]

        # RBF component to cross covariance matrix. Start by computing
        # Euclidean distances between testing and training inputs
        wb = np.sqrt((-2*(Z_tr@Z_te.T).T + np.sum(Z_tr*Z_tr, axis=1)).T + np.sum(Z_te*Z_te, axis=1))

        # Figure out a way to read kernel type from M["kernel"]["k"]
        # Matern32
        wb = np.sqrt(3.) / theta[1] * wb  # h in kernel_functions.jl
        wb = theta[0] * (1. + wb) * np.exp(-wb)
        # Matern52
        # wb = sqrt(5.) / theta[1] * wb # h in kernel_functions.jl
        # wb = theta[0] * (1. + wb + wb**2 / sqrt(3.)) * exp(-wb)

        # linear component to cross covariance matrix
        wb2 = theta[2] * Z_tr @ Z_te.T
        return (wb + wb2).T @ M["h"][:]

    def reduce_points(self, points, Xproj, Xmu, Xsigma):
        Z = (points - Xmu) / Xsigma
        H = (Xproj["vectors"][:, :].T / Xproj["values"][:])
        return Z @ H
