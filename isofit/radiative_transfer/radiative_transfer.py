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
# Authors: Philip G. Brodrick, philip.brodrick@jpl.nasa.gov
#          Niklas Bohn, urs.n.bohn@jpl.nasa.gov
#          Jay E. Fahlen, jay.e.fahlen@jpl.nasa.gov
#
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np

from isofit.core.common import eps
from isofit.radiative_transfer.engines import Engines

Logger = logging.getLogger(__file__)


def confPriority(key, configs):
    """
    Selects a key from a config if the value for that key is not None
    Prioritizes returning the first value found in the configs list

    TODO: ISOFIT configs are annoying and will create keys to NoneTypes
    Should use mlky to handle key discovery at runtime instead of like this
    """
    value = None
    for config in configs:
        if hasattr(config, key):
            value = getattr(config, key)
            if value is not None:
                break
    return value


class RadiativeTransfer:
    """This class controls the radiative transfer component of the forward
    model. An ordered dictionary is maintained of individual RTMs (MODTRAN,
    for example). We loop over the dictionary concatenating the radiation
    and derivatives from each RTM and interval to form the complete result.

    In general, some of the state vector components will be shared between
    RTMs and bands. For example, H20STR is shared between both VISNIR and
    TIR. This class maintains the master list of statevectors.
    """

    # Keys to retrieve from 3 sections to use the preferred
    # Prioritizes retrieving from radiative_transfer_engines first, then instrument, then radiative_transfer
    _keys = [
        "interpolator_style",
        "overwrite_interpolator",
        "lut_grid",
        "lut_path",
        "wavelength_file",
    ]

    def __init__(self, full_config: Config):
        config = full_config.forward_model.radiative_transfer
        confIT = full_config.forward_model.instrument

        self.lut_grid = config.lut_grid
        self.statevec_names = config.statevector.get_element_names()

        self.rt_engines = []
        for idx in range(len(config.radiative_transfer_engines)):
            confRT = config.radiative_transfer_engines[idx]

            if confRT.engine_name not in Engines:
                raise AttributeError(
                    f"Invalid radiative transfer engine choice. Got: {confRT.engine_name}; Must be one of: {RTE}"
                )

            # Generate the params for this RTE
            params = {
                key: confPriority(key, [confRT, confIT, config]) for key in self._keys
            }
            params["engine_config"] = confRT

            # Select the right RTE and initialize it
            rte = Engines[confRT.engine_name](**params)
            self.rt_engines.append(rte)

            # Make sure the length of the config statevectores match the engine's assumed statevectors
            if (expected := len(config.statevector.get_element_names())) != (
                got := len(rte.indices.x_RT)
            ):
                error = f"Mismatch between the number of elements for the config statevector and LUT.indices.x_RT: {expected=}, {got=}"
                Logger.error(error)
                raise AttributeError(error)

        # If any engine is true, self is true
        self.topography_model = any([rte.topography_model for rte in self.rt_engines])
        self.glint_model = any([rte.glint_model for rte in self.rt_engines])

        # The rest of the code relies on sorted order of the individual RT engines which cannot
        # be guaranteed by the dict JSON or YAML input
        self.rt_engines.sort(key=lambda x: x.wl[0])

        # Retrieved variables.  We establish scaling, bounds, and
        # initial guesses for each state vector element.  The state
        # vector elements are all free parameters in the RT lookup table,
        # and they all have associated dimensions in the LUT grid.
        self.bounds, self.scale, self.init = [], [], []
        self.prior_mean, self.prior_sigma = [], []

        for sv, sv_name in zip(*config.statevector.get_elements()):
            self.bounds.append(sv.bounds)
            self.scale.append(sv.scale)
            self.init.append(sv.init)
            self.prior_sigma.append(sv.prior_sigma)
            self.prior_mean.append(sv.prior_mean)

        self.bounds = np.array(self.bounds)
        self.scale = np.array(self.scale)
        self.init = np.array(self.init)
        self.prior_mean = np.array(self.prior_mean)
        self.prior_sigma = np.array(self.prior_sigma)

        self.wl = np.concatenate([RT.wl for RT in self.rt_engines])

        self.bvec = config.unknowns.get_element_names()
        self.bval = np.array([x for x in config.unknowns.get_elements()[0]])

        self.solar_irr = np.concatenate([RT.solar_irr for RT in self.rt_engines])

        # radiances along all optical paths
        self.L_coupled = []

    def xa(self):
        """Pull the priors from each of the individual RTs."""
        return self.prior_mean

    def Sa(self):
        """Pull the priors from each of the individual RTs."""
        return np.diagflat(np.power(np.array(self.prior_sigma), 2))

    def get_shared_rtm_quantities(self, x_RT, geom):
        """Return only the set of RTM quantities (transup, sphalb, etc.) that are contained
        in all RT engines.
        """
        ret = []
        for RT in self.rt_engines:
            ret.append(RT.get(x_RT, geom))

        return self.pack_arrays(ret)

    @property
    def coszen(self):
        """
        Backwards compatibility until Geometry takes over this param
        Return some child RTE coszen
        """
        for child in self.rt_engines:
            if "coszen" in child.lut:
                return child.lut.coszen.data

    def calc_rdn(self, x_RT, rfl_dir, rfl_dif, Ls, geom):
        """
        Physics-based forward model to calculate at-sensor radiance.
        Includes topography, background reflectance, and glint.
        """
        # ToDo: get TOA solar zenith angle from geometry object or from self?
        coszen = (
            np.cos(np.deg2rad(geom.solar_zenith))
            if np.isnan(self.coszen)
            else self.coszen
        )

        # local solar zenith angle as a function of surface slope and aspect
        cos_i = geom.cos_i if geom.cos_i is not None else coszen

        # get needed rt quantities from LUT
        r = self.get_shared_rtm_quantities(x_RT, geom)

        # atmospheric path radiance
        L_atm = self.get_L_atm(x_RT, geom)

        # atmospheric spherical albedo
        s_alb = r["sphalb"]

        # radiances along all optical paths
        L_bi_direct, L_hemi_direct, L_direct_hemi, L_bi_hemi = self.get_L_coupled(
            r, coszen, cos_i
        )

        # thermal transmittance
        L_up = Ls * (r["transm_up_dir"] + r["transm_up_dif"])

        # adjacency effects
        # ToDo: we need to think about if we want to obtain the background reflectance from the Geometry object
        #  or from the surface model, i.e., the same way as we do with the target pixel reflectance
        bg_dir = geom.bg_rfl if geom.bg_rfl is not None else rfl_dir
        bg_dif = geom.bg_rfl if geom.bg_rfl is not None else rfl_dif

        # at-sensor radiance model, including topography, adjacency effects, and glint
        ret = (
            L_atm
            + (
                L_bi_direct * rfl_dir  # bi-directional radiance
                + L_hemi_direct * rfl_dif  # hemispherical-directional radiance
                + L_direct_hemi * bg_dir  # directional-hemispherical radiance
                + L_bi_hemi * bg_dif  # bi-hemispherical radiance
            )
            / (1.0 - s_alb * bg_dif)
            + L_up
        )

        return ret

    def rdn_to_rho(self, rdn, solar_irr=None):
        """Function to convert a radiance vector to transmittance.

        Args:
            rdn:       input data vector in radiance
            solar_irr: solar irradiance vector (optional)

        Returns:
            Data vector converted to transmittance
        """
        try:
            rho = rdn * np.pi / (solar_irr * self.coszen)
        except TypeError:
            rho = rdn * np.pi / (self.solar_irr * self.coszen)
        return rho

    def rho_to_rdn(self, rho, solar_irr=None):
        """Function to convert a transmittance vector to radiance.

        Args:
            rho:       input data vector in transmittance
            solar_irr: solar irradiance vector (optional)

        Returns:
            Data vector converted to radiance
        """
        try:
            rdn = (solar_irr * self.coszen) / np.pi * rho
        except TypeError:
            rdn = (self.solar_irr * self.coszen) / np.pi * rho
        return rdn

    def get_L_atm(self, x_RT: np.array, geom: Geometry) -> np.array:
        """Get the interpolated modeled atmospheric path radiance.

        Args:
            x_RT: radiative-transfer portion of the statevector
            geom: local geometry conditions for lookup

        Returns:
            interpolated modeled atmospheric path radiance
        """
        L_atms = []
        for RT in self.rt_engines:
            if RT.treat_as_emissive:
                r = RT.get(x_RT, geom)
                rdn = r["thermal_upwelling"]
                L_atms.append(rdn)
            else:
                r = RT.get(x_RT, geom)
                if RT.rt_mode == "rdn":
                    L_atm = r["rhoatm"]
                else:
                    rho_atm = r["rhoatm"]
                    L_atm = self.rho_to_rdn(rho_atm)
                L_atms.append(L_atm)
        return np.hstack(L_atms)

    def get_L_down_transmitted(self, x_RT: np.array, geom: Geometry) -> np.array:
        """Get the interpolated total, direct, and diffuse downward atmospheric radiance.
        Thermal_downwelling already includes the transmission factor. Also
        assume there is no multiple scattering for TIR.

        Args:
            x_RT: radiative-transfer portion of the statevector
            geom: local geometry conditions for lookup

        Returns:
            interpolated total, direct, and diffuse downward atmospheric radiance
        """
        L_downs = []
        L_downs_dir = []
        L_downs_dif = []

        for RT in self.rt_engines:
            if RT.treat_as_emissive:
                r = RT.get(x_RT, geom)
                rdn = r["thermal_downwelling"]
                L_downs.append(rdn)
            else:
                r = RT.get(x_RT, geom)
                if RT.rt_mode == "rdn":
                    L_down_dir = r["transm_down_dir"]
                    L_down_dif = r["transm_down_dif"]
                else:
                    # Transform downward transmittance to radiance
                    L_down_dir = self.rho_to_rdn(r["transm_down_dir"])
                    L_down_dif = self.rho_to_rdn(r["transm_down_dif"])

                L_down = L_down_dir + L_down_dif

                L_downs.append(L_down)
                L_downs_dir.append(L_down_dir)
                L_downs_dif.append(L_down_dif)

        return np.hstack(L_downs), np.hstack(L_downs_dir), np.hstack(L_downs_dif)

    def get_L_coupled(self, r, coszen, cos_i):
        """Get the interpolated radiances on the sun-to-surface-to-sensor path.
        These follow the nomenclature as presented by Schaepman-Strub et al. (2006),
        which essentially are the terms from Nicodemus et al. (1977),
        but adapted to the remote sensing case by Martonchik et al. (2000).

        Args:
            r:      interpolated radiative transfer quantities from the LUT
            coszen: top-of-atmosphere solar zenith angle
            cos_i:  local solar zenith angle at the surface

        Returns:
            interpolated radiances along all optical paths:
            bi-directional            (downward direct * upward direct)
            hemispherical-directional ((downward direct + diffuse) * upward direct)
            directional-hemispherical (downward direct * upward diffuse)
            bi-hemispherical          ((downward direct + diffuse) * upward diffuse)
        """
        if any(
            [type(r[key]) != np.ndarray for key in self.rt_engines[0].coupling_terms]
        ):
            self.L_coupled = [
                r["transm_down_dir"],
                r["transm_down_dir"] + r["transm_down_dif"],
                0,
                0,
            ]
        else:
            for key in self.rt_engines[0].coupling_terms:
                self.L_coupled.append(
                    self.solar_irr * coszen / np.pi * r[key]
                    if self.rt_engines[0].rt_mode == "transm"
                    else r[key]
                )

        # unscaling and rescaling downward direct radiance by local solar zenith angle (see above)
        for ind in [0, 2]:
            self.L_coupled[ind] = self.L_coupled[ind] / coszen * cos_i

        L_bi_direct = self.L_coupled[0]
        L_hemi_direct = self.L_coupled[1]
        L_direct_hemi = self.L_coupled[2]
        L_bi_hemi = self.L_coupled[3]

        return L_bi_direct, L_hemi_direct, L_direct_hemi, L_bi_hemi

    def drdn_dRT(
        self,
        x_RT,
        x_surface,
        rfl_dir,
        rfl_dif,
        drfl_dsurface,
        Ls,
        dLs_dsurface,
        geom: Geometry,
    ):
        # first the rdn at the current state vector
        rdn = self.calc_rdn(x_RT, rfl_dir, rfl_dif, Ls, geom)

        # perturb each element of the RT state vector (finite difference)
        K_RT = []
        x_RTs_perturb = x_RT + np.eye(len(x_RT)) * eps
        for x_RT_perturb in list(x_RTs_perturb):
            rdne = self.calc_rdn(x_RT_perturb, rfl_dir, rfl_dif, Ls, geom)
            K_RT.append((rdne - rdn) / eps)
        K_RT = np.array(K_RT).T

        # Get K_surface
        # local solar zenith angle as a function of surface slope and aspect
        cos_i = geom.cos_i if geom.cos_i is not None else self.coszen

        # get needed rt quantities from LUT
        r = self.get_shared_rtm_quantities(x_RT, geom)

        # atmospheric spherical albedo
        s_alb = r["sphalb"]

        # direct and diffuse downward radiance on the sun-to-surface path
        # note: currently, L_down_dir comes scaled by the TOA solar zenith angle,
        # thus, unscaling and rescaling by local solar zenith angle required
        # to account for surface slope and aspect
        L_down_tot, L_down_dir, L_down_dif = self.get_L_down_transmitted(x_RT, geom)
        L_down_dir = L_down_dir / self.coszen * cos_i

        # including glint for water surfaces
        if self.glint_model:
            L_sky = x_surface[-2] * L_down_dir + x_surface[-1] * L_down_dif

            rho_ls = 0.02  # fresnel reflectance factor (approx. 0.02 for nadir view)
            glint = rho_ls * (L_sky / L_down_tot)
        else:
            glint = np.zeros(rfl_dir.shape)

        # adjacency effects
        bg = geom.bg_rfl if geom.bg_rfl is not None else rfl_dir + glint

        # K surface reflectance
        drdn_drfl = (L_down_dir + L_down_dif) / (1.0 - s_alb * bg) * r["transm_up_dir"]

        drdn_dLs = r["transm_up_dir"] + r["transm_up_dif"]

        K_surface = (
            drdn_drfl[:, np.newaxis] * drfl_dsurface
            + drdn_dLs[:, np.newaxis] * dLs_dsurface
        )

        if self.glint_model:
            # K glint
            drdn_dgdd = (
                L_down_dir
                * (r["transm_up_dir"] + r["transm_up_dif"])
                / (1.0 - s_alb * bg)
            )
            drdn_dgdsf = (
                L_down_dif
                * (r["transm_up_dir"] + r["transm_up_dif"])
                / (1.0 - s_alb * bg)
            )

            K_surface[:, -2] = drdn_dgdd
            K_surface[:, -1] = drdn_dgdsf

        return K_RT, K_surface

    def drdn_dRTb(self, x_RT, rfl_dir, rfl_dif, Ls, geom):
        if len(self.bvec) == 0:
            Kb_RT = np.zeros((0, len(self.wl.shape)))
        # currently, the K_b matrix only covers forward model derivatives due to H2O_ABSCO unknowns,
        # so that subsequent errors might occur when water vapor is not part of the state
        # vector (which is very unlikely though). the following statement captures this case,
        # but might need to be modified as soon as we add more unknowns
        # ToDo: might require modification in case more unknowns are added
        elif len(self.bvec) > 0 and "H2OSTR" not in self.statevec_names:
            Kb_RT = np.zeros((1, len(self.wl)))
        else:
            # first the radiance at the current state vector
            rdn = self.calc_rdn(x_RT, rfl_dir, rfl_dif, Ls, geom)

            # unknown parameters modeled as random variables per
            # Rodgers et al (2000) K_b matrix.  We calculate these derivatives
            # by finite differences
            Kb_RT = []
            perturb = 1.0 + eps
            for unknown in self.bvec:
                if unknown == "H2O_ABSCO" and "H2OSTR" in self.statevec_names:
                    i = self.statevec_names.index("H2OSTR")
                    x_RT_perturb = x_RT.copy()
                    x_RT_perturb[i] = x_RT[i] * perturb
                    rdne = self.calc_rdn(x_RT_perturb, rfl_dir, rfl_dif, Ls, geom)
                    Kb_RT.append((rdne - rdn) / eps)

        Kb_RT = np.array(Kb_RT).T
        return Kb_RT

    @staticmethod
    def fresnel_rf(vza):
        """Calculates reflectance factor of sky radiance based on the
        Fresnel equation for unpolarized light as a function of view zenith angle (vza).
        """
        if vza > 0.0:
            n_w = 1.33  # refractive index of water
            theta = np.deg2rad(vza)

            # calculate angle of refraction using Snell′s law
            theta_i = np.arcsin(np.sin(theta) / n_w)

            # reflectance factor of sky radiance based on the Fresnel equation for unpolarized light
            rho_s = 0.5 * np.abs(
                ((np.sin(theta - theta_i) ** 2) / (np.sin(theta + theta_i) ** 2))
                + ((np.tan(theta - theta_i) ** 2) / (np.tan(theta + theta_i) ** 2))
            )
        else:
            rho_s = 0.02  # the reflectance factor converges to 0.02 for view angles equal to 0.0°

        return rho_s

    def summarize(self, x_RT, geom):
        ret = []
        for RT in self.rt_engines:
            ret.append(RT.summarize(x_RT, geom))
        ret = "\n".join(ret)
        return ret

    def pack_arrays(self, rtm_quantities_from_RT_engines):
        """Take the list of dict outputs from each RT engine and
        stack their internal arrays in the same order. Keep only
        those quantities that are common to all RT engines.
        """
        # Get the intersection of the sets of keys from each of the rtm_quantities_from_RT_engines
        shared_rtm_keys = set(rtm_quantities_from_RT_engines[0].keys())
        if len(rtm_quantities_from_RT_engines) > 1:
            for rtm_quantities_from_one_RT_engine in rtm_quantities_from_RT_engines[1:]:
                shared_rtm_keys.intersection_update(
                    rtm_quantities_from_one_RT_engine.keys()
                )

        # Concatenate the different band ranges
        rtm_quantities_concatenated_over_RT_bands = {}
        for key in shared_rtm_keys:
            temp = [x[key] for x in rtm_quantities_from_RT_engines]
            rtm_quantities_concatenated_over_RT_bands[key] = np.hstack(temp)

        return rtm_quantities_concatenated_over_RT_bands


def ext550_to_vis(ext550):
    """VIS is defined as a function of the surface aerosol extinction coefficient
    at 550 nm in km-1, EXT550, by the formula VIS[km] = ln(50) / (EXT550 + 0.01159),
    where 0.01159 is the surface Rayleigh scattering coefficient at 550 nm in km-1
    (see MODTRAN6 manual, p. 50).
    """
    return np.log(50.0) / (ext550 + 0.01159)
