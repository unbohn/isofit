#! /usr/bin/env python3
#
#  Copyright 2018 California Institute of Technology
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
#

import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.linalg import block_diag

from ..core.common import table_to_array
from .surface_multicomp import MultiComponentSurface
from isofit.configs import Config


class LiquidWaterSurface(MultiComponentSurface):
    """A model of the surface based on a collection of multivariate
       Gaussians, extended with a surface liquid water term."""

    def __init__(self, full_config: Config):

        super().__init__(full_config)

        self.lw_feature_left = np.argmin(abs(850 - self.wl))
        self.lw_feature_right = np.argmin(abs(1100 - self.wl))
        self.wl_sel = self.wl[self.lw_feature_left:self.lw_feature_right + 1]

        # TODO: Enforce this attribute in the config, not here (this is hidden)
        self.statevec_names.extend(['Liquid_Water', 'Intercept', 'Slope'])
        self.scale.extend([1.0, 1.0, 1.0])
        self.init.extend([0.02, 0.3, 0.0002])
        self.bounds.extend([[0, 0.5], [0, 1.0], [-0.0004, 0.0004]])

        self.n_state = self.n_state + 3
        self.lw_ind = len(self.statevec_names) - 3

        self.k_wi = pd.read_excel(io=self.path_k, engine='openpyxl')
        self.wvl_water, self.k_water = table_to_array(k_wi=self.k_wi, a=0, b=982, col_wvl="wvl_6", col_k="T = 20°C")
        self.kw = np.interp(x=self.wl_sel, xp=self.wvl_water, fp=self.k_water)
        self.abs_co_w = 4 * np.pi * self.kw / self.wl_sel

    def xa(self, x_surface, geom):
        """Mean of prior distribution, calculated at state x."""

        mu = MultiComponentSurface.xa(self, x_surface, geom)
        mu[self.lw_ind:] = self.init[self.lw_ind:]
        return mu

    def Sa(self, x_surface, geom):
        """Covariance of prior distribution, calculated at state x.  We find
        the covariance in a normalized space (normalizing by z) and then un-
        normalize the result for the calling function."""

        Cov = MultiComponentSurface.Sa(self, x_surface, geom)
        f = (1000 * np.array(self.scale[self.lw_ind:])) ** 2
        Cov[self.lw_ind:, self.lw_ind:] = np.diag(f)

        return Cov

    def fit_params(self, rfl_meas, geom, *args):
        """Given a reflectance estimate, fit a state vector including surface liquid water."""

        rfl_meas_sel = rfl_meas[self.lw_feature_left:self.lw_feature_right+1]

        x_opt = least_squares(
            fun=self.err_obj,
            x0=self.init[self.lw_ind:],
            jac='2-point',
            method='trf',
            bounds=(np.array([self.bounds[ii][0] for ii in range(3)]),
                    np.array([self.bounds[ii][1] for ii in range(3)])),
            max_nfev=15,
            args=(rfl_meas_sel,)
        )

        x = MultiComponentSurface.fit_params(self, rfl_meas, geom)
        x[self.lw_ind:] = x_opt.x
        return x

    def err_obj(self, x, y):
        """Function, which computes the vector of residuals between measured and modeled surface reflectance optimizing
        for path length of surface liquid water."""

        attenuation = np.exp(-x[0] * 1e7 * self.abs_co_w)
        rho = (x[1] + x[2] * self.wl_sel) * attenuation
        resid = rho - y
        return resid

    def calc_rfl(self, x_surface, geom):
        """Returns lamberatian reflectance for a given state."""

        return self.calc_lamb(x_surface, geom)

    def drfl_dsurface(self, x_surface, geom):
        """Partial derivative of reflectance with respect to state vector,
        calculated at x_surface."""

        drfl = self.dlamb_dsurface(x_surface, geom)
        drfl[:, self.lw_ind:] = 1
        return drfl

    def summarize(self, x_surface, geom):
        """Summary of state vector."""

        return MultiComponentSurface.summarize(self, x_surface, geom) + \
            ' Liquid Water: %5.3f' % x_surface[self.lw_ind]
