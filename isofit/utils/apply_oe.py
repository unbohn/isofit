#! /usr/bin/env python3
#
# Authors: David R Thompson and Philip G. Brodrick
#

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from os.path import abspath, exists, join, split
from shutil import copyfile
from sys import platform
from types import SimpleNamespace
from typing import List
from warnings import warn

import click
import numpy as np
from sklearn import mixture
from spectral.io import envi

from isofit.core import common, isofit
from isofit.core.common import envi_header
from isofit.utils import analytical_line, empirical_line, extractions, segment

warn(
    message=(
        f"The module {__name__} is deprecated and will be removed with ISOFIT version"
        " 3.2."
    ),
    category=DeprecationWarning,
    stacklevel=2,
)

EPS = 1e-6
CHUNKSIZE = 256

UNCORRELATED_RADIOMETRIC_UNCERTAINTY = 0.01

INVERSION_WINDOWS = [[350.0, 1360.0], [1410, 1800.0], [1970.0, 2500.0]]


def apply_oe(args):
    """This is a helper script to apply OE over a flightline using the MODTRAN radiative transfer engine.

    The goal is to run isofit in a fairly 'standard' way, accounting for the types of variation that might be
    considered typical.  For instance, we use the observation (obs) and location (loc) files to determine appropriate
    MODTRAN view-angle geometry look up tables, and provide a heuristic means of determing atmospheric water ranges.

    This code also proivdes the capicity for speedup through the empirical line solution.

    Args:
        input_radiance (str): radiance data cube [expected ENVI format]
        input_loc (str): location data cube, (Lon, Lat, Elevation) [expected ENVI format]
        input_obs (str): observation data cube, (path length, to-sensor azimuth, to-sensor zenith, to-sun azimuth,
            to-sun zenith, phase, slope, aspect, cosine i, UTC time) [expected ENVI format]
        working_directory (str): directory to stage multiple outputs, will contain subdirectories
        sensor (str): the sensor used for acquisition, will be used to set noise and datetime settings.  choices are:
            [ang, avcl, neon, prism]
        copy_input_files (Optional, int): flag to choose to copy input_radiance, input_loc, and input_obs locally into
            the working_directory.  0 for no, 1 for yes.  Default 0
        modtran_path (Optional, str): Location of MODTRAN utility, alternately set with MODTRAN_DIR environment variable
        wavelength_path (Optional, str): Location to get wavelength information from, if not specified the radiance
            header will be used
        surface_category (Optional, str): The type of isofit surface priors to use.  Default is multicomponent_surface
        aerosol_climatology_path (Optional, str): Specific aerosol climatology information to use in MODTRAN,
            default None
        rdn_factors_path (Optional, str): Specify a radiometric correction factor, if desired. default None
        surface_path (Optional, str): Path to surface model - required if surface is multicomponent_surface (default
            above).  Alternately set with ISOFIT_SURFACE_MODEL environment variable. default None
        channelized_uncertainty_path (Optional, str): path to a channelized uncertainty file.  default None
        lut_config_file (Optional, str): Path to a look up table configuration file, which will override defaults
            chocies. default None
        logging_level (Optional, str): Logging level with which to run isofit.  Default INFO
        log_file (Optional, str): File path to write isofit logs to.  Default None
        n_cores (Optional, int): Number of cores to run isofit with.  Substantial parallelism is available, and full
            runs will be very slow in serial.  Suggested to max this out on the available system.  Default 1
        presolve (Optional, int): Flag to use a presolve mode to estimate the available atmospheric water range.  Runs
            a preliminary inversion over the image with a 1-D LUT of water vapor, and uses the resulting range (slightly
            expanded) to bound determine the full LUT.  Advisable to only use with small cubes or in concert with the
            empirical_line setting, or a significant speed penalty will be incurred.  Choices - 0 off, 1 on. Default 0
        empirical_line (Optional, int): Use an empirical line interpolation to run full inversions over only a subset
            of pixels, determined using a SLIC superpixel segmentation, and use a KDTREE Of local solutions to
            interpolate radiance->reflectance.  Generally a good option if not trying to analyze the atmospheric state
            at fine scale resolution.  Choices - 0 off, 1 on.  Default 0
        ray_temp_dir (Optional, str): Location of temporary directory for ray parallelization engine.  Default is
            '/tmp/ray'
        emulator_base (Optional, str): Location of emulator base path.  Point this at the model folder (or h5 file) of
            sRTMnet to use the emulator instead of MODTRAN.  An additional file with the same basename and the extention
            _aux.npz must accompany (e.g. /path/to/emulator.h5 /path/to/emulator_aux.npz)
        segmentation_size (Optional, int): Size of segments to construct for empirical line (if used).
        num_neighbors (Optional, int): Forced number of neighbors for empirical line extrapolation - overides default
            set from segmentation_size parameter.
        pressure_elevation (Optional, bool): If set, retrieves elevation.

            Reference:
            D.R. Thompson, A. Braverman,P.G. Brodrick, A. Candela, N. Carbon, R.N. Clark,D. Connelly, R.O. Green, R.F.
            Kokaly, L. Li, N. Mahowald, R.L. Miller, G.S. Okin, T.H.Painter, G.A. Swayze, M. Turmon, J. Susilouto, and
            D.S. Wettergreen. Quantifying Uncertainty for Remote Spectroscopy of Surface Composition. Remote Sensing of
            Environment, 2020. doi: https://doi.org/10.1016/j.rse.2020.111898.

            Emulator reference:
            P.G. Brodrick, D.R. Thompson, J.E. Fahlen, M.L. Eastwood, C.M. Sarture, S.R. Lundeen, W. Olson-Duvall,
            N. Carmon, and R.O. Green. Generalized radiative transfer emulation for imaging spectroscopy reflectance
            retrievals. Remote Sensing of Environment, 261:112476, 2021.doi: 10.1016/j.rse.2021.112476.


    Returns:

    """
    use_superpixels = (args.empirical_line == 1) or (args.analytical_line == 1)

    if args.sensor not in [
        "ang",
        "av3",
        "avcl",
        "emit",
        "enmap",
        "hyp",
        "neon",
        "prism",
        "prisma",
    ]:
        if args.sensor[:3] != "NA-":
            raise ValueError(
                'argument sensor: invalid choice: "NA-test" (choose from '
                '"ang", "av3", "avcl", "emit", "enmap", "hyp", "neon", "prism", "prisma", "NA-*")'
            )

    if args.num_neighbors is not None and len(args.num_neighbors) > 1:
        if args.analytical_line != 1:
            raise ValueError(
                "If num_neighbors has multiple elements, --analytical_line must equal 1"
            )
        if args.empirical_line == 1:
            raise ValueError(
                "If num_neighbors has multiple elements, only --analytical_line is valid"
            )

    if args.copy_input_files == 1:
        args.copy_input_files = True
    else:
        args.copy_input_files = False

    logging.basicConfig(
        format="%(levelname)s:%(asctime)s ||| %(message)s",
        level=args.logging_level,
        filename=args.log_file,
        datefmt="%Y-%m-%d,%H:%M:%S",
    )
    logging.info(args)

    rdn_dataset = envi.open(envi_header(args.input_radiance))
    rdn_size = (rdn_dataset.shape[0], rdn_dataset.shape[1])
    del rdn_dataset
    for infile_name, infile in zip(
        ["input_radiance", "input_loc", "input_obs"],
        [args.input_radiance, args.input_loc, args.input_obs],
    ):
        if os.path.isfile(infile) is False:
            err_str = (
                f"Input argument {infile_name} give as: {infile}.  File not found on"
                " system."
            )
            raise ValueError("argument " + err_str)
        if infile_name != "input_radiance":
            input_dataset = envi.open(envi_header(infile), infile)
            input_size = (input_dataset.shape[0], input_dataset.shape[1])
            if not (input_size[0] == rdn_size[0] and input_size[1] == rdn_size[1]):
                err_str = (
                    f"Input file: {infile_name} size is {input_size}, which does not"
                    f" match input_radiance size: {rdn_size}"
                )
                raise ValueError(err_str)

    lut_params = LUTConfig(args.lut_config_file)
    if args.emulator_base is not None:
        lut_params.aot_550_range = lut_params.aerosol_2_range
        lut_params.aot_550_spacing = lut_params.aerosol_2_spacing
        lut_params.aot_550_spacing_min = lut_params.aerosol_2_spacing_min
        lut_params.aerosol_2_spacing = 0

    paths = Pathnames(args)
    paths.make_directories()
    paths.stage_files()

    # Based on the sensor type, get appropriate year/month/day info fro intial condition.
    # We'll adjust for line length and UTC day overrun later
    if args.sensor == "ang":
        # parse flightline ID (AVIRIS-NG assumptions)
        dt = datetime.strptime(paths.fid[3:], "%Y%m%dt%H%M%S")
    elif args.sensor == "av3":
        # parse flightline ID (AVIRIS-3 assumptions)
        dt = datetime.strptime(paths.fid[3:], "%Y%m%dt%H%M%S")
    elif args.sensor == "avcl":
        # parse flightline ID (AVIRIS-Classic assumptions)
        dt = datetime.strptime("20{}t000000".format(paths.fid[1:7]), "%Y%m%dt%H%M%S")
    elif args.sensor == "emit":
        # parse flightline ID (EMIT assumptions)
        dt = datetime.strptime(paths.fid[:19], "emit%Y%m%dt%H%M%S")
        global INVERSION_WINDOWS
        INVERSION_WINDOWS = [[380.0, 1325.0], [1435, 1770.0], [1965.0, 2500.0]]
    elif args.sensor == "enmap":
        # parse flightline ID (EnMAP assumptions)
        dt = datetime.strptime(paths.fid[:15], "%Y%m%dt%H%M%S")
    elif args.sensor == "hyp":
        # parse flightline ID (Hyperion assumptions)
        dt = datetime.strptime(paths.fid[10:17], "%Y%j")
    elif args.sensor == "neon":
        # parse flightline ID (NEON assumptions)
        dt = datetime.strptime(paths.fid, "NIS01_%Y%m%d_%H%M%S")
    elif args.sensor == "prism":
        # parse flightline ID (PRISM assumptions)
        dt = datetime.strptime(paths.fid[3:], "%Y%m%dt%H%M%S")
    elif args.sensor == "prisma":
        # parse flightline ID (PRISMA assumptions)
        dt = datetime.strptime(paths.fid, "%Y%m%d%H%M%S")
    elif args.sensor[:3] == "NA-":
        dt = datetime.strptime(args.sensor[3:], "%Y%m%d")
    else:
        raise ValueError(
            "Datetime object could not be obtained. Please check file name of input"
            " data."
        )

    dayofyear = dt.timetuple().tm_yday

    (
        h_m_s,
        day_increment,
        mean_path_km,
        mean_to_sensor_zenith,
        mean_to_sun_zenith,
        mean_to_sun_azimuth,
        mean_relative_azimuth,
        valid,
        to_sensor_zenith_lut_grid,
        to_sun_zenith_lut_grid,
        relative_azimuth_lut_grid,
    ) = get_metadata_from_obs(paths.obs_working_path, lut_params)

    # overwrite the time in case original obs has an error in that band
    if h_m_s[0] != dt.hour and h_m_s[0] >= 24:
        h_m_s[0] = dt.hour
        logging.info(
            "UTC hour did not match start time minute. Adjusting to that value."
        )
    if h_m_s[1] != dt.minute and h_m_s[1] >= 60:
        h_m_s[1] = dt.minute
        logging.info(
            "UTC minute did not match start time minute. Adjusting to that value."
        )

    if day_increment:
        dayofyear += 1

    gmtime = float(h_m_s[0] + h_m_s[1] / 60.0)

    # get radiance file, wavelengths
    if args.wavelength_path:
        chn, wl, fwhm = np.loadtxt(args.wavelength_path).T
    else:
        radiance_dataset = envi.open(envi_header(paths.radiance_working_path))
        wl = np.array([float(w) for w in radiance_dataset.metadata["wavelength"]])
        if "fwhm" in radiance_dataset.metadata:
            fwhm = np.array([float(f) for f in radiance_dataset.metadata["fwhm"]])
        elif "FWHM" in radiance_dataset.metadata:
            fwhm = np.array([float(f) for f in radiance_dataset.metadata["FWHM"]])
        else:
            fwhm = np.ones(wl.shape) * (wl[1] - wl[0])

        # Close out radiance dataset to avoid potential confusion
        del radiance_dataset

    # Convert to microns if needed
    if wl[0] > 100:
        wl = wl / 1000.0
        fwhm = fwhm / 1000.0

    # write wavelength file
    wl_data = np.concatenate(
        [np.arange(len(wl))[:, np.newaxis], wl[:, np.newaxis], fwhm[:, np.newaxis]],
        axis=1,
    )
    np.savetxt(paths.wavelength_path, wl_data, delimiter=" ")

    (
        mean_latitude,
        mean_longitude,
        mean_elevation_km,
        elevation_lut_grid,
    ) = get_metadata_from_loc(
        paths.loc_working_path, lut_params, pressure_elevation=args.pressure_elevation
    )

    if lut_params.flag_ocean_elevation:
        elevation_lut_grid = None
        mean_elevation_km = 0.0

    if args.emulator_base is not None:
        if elevation_lut_grid is not None and np.any(elevation_lut_grid < 0):
            to_rem = elevation_lut_grid[elevation_lut_grid < 0].copy()
            elevation_lut_grid[elevation_lut_grid < 0] = 0
            elevation_lut_grid = np.unique(elevation_lut_grid)
            if len(elevation_lut_grid) == 1:
                elevation_lut_grid = None
                mean_elevation_km = elevation_lut_grid[
                    0
                ]  # should be 0, but just in case
            logging.info(
                "Scene contains target lut grid elements < 0 km, and uses 6s (via"
                " sRTMnet).  6s does not support targets below sea level in km units. "
                f" Setting grid points {to_rem} to 0."
            )
        if mean_elevation_km < 0:
            mean_elevation_km = 0
            logging.info(
                f"Scene contains a mean target elevation < 0.  6s does not support"
                f" targets below sea level in km units.  Setting mean elevation to 0."
            )

    # Need a 180 - here, as this is already in MODTRAN convention
    mean_altitude_km = (
        mean_elevation_km
        + np.cos(np.deg2rad(180 - mean_to_sensor_zenith)) * mean_path_km
    )

    logging.info("Observation means:")
    logging.info(f"Path (km): {mean_path_km}")
    logging.info(f"To-sensor zenith (deg): {mean_to_sensor_zenith}")
    logging.info(f"To-sun zenith (deg): {mean_to_sun_zenith}")
    logging.info(f"To-sun azimuth (deg): {mean_to_sun_azimuth}")
    logging.info(f"Relative to-sun azimuth (deg): {mean_relative_azimuth}")
    logging.info(f"Altitude (km): {mean_altitude_km}")

    if args.emulator_base is not None and mean_altitude_km > 99:
        logging.info(
            "Adjusting altitude to 99 km for integration with 6S, because emulator is"
            " chosen."
        )
        mean_altitude_km = 99

    # We will use the model discrepancy with covariance OR uncorrelated
    # Calibration error, but not both.
    if args.model_discrepancy_path is not None:
        uncorrelated_radiometric_uncertainty = 0
    else:
        uncorrelated_radiometric_uncertainty = UNCORRELATED_RADIOMETRIC_UNCERTAINTY

    # Superpixel segmentation
    if use_superpixels:
        if not exists(paths.lbl_working_path) or not exists(
            paths.radiance_working_path
        ):
            logging.info("Segmenting...")
            segment(
                spectra=(paths.radiance_working_path, paths.lbl_working_path),
                nodata_value=-9999,
                npca=5,
                segsize=args.segmentation_size,
                nchunk=CHUNKSIZE,
                n_cores=args.n_cores,
                loglevel=args.logging_level,
                logfile=args.log_file,
            )

        # Extract input data per segment
        for inp, outp in [
            (paths.radiance_working_path, paths.rdn_subs_path),
            (paths.obs_working_path, paths.obs_subs_path),
            (paths.loc_working_path, paths.loc_subs_path),
        ]:
            if not exists(outp):
                logging.info("Extracting " + outp)
                extractions(
                    inputfile=inp,
                    labels=paths.lbl_working_path,
                    output=outp,
                    chunksize=CHUNKSIZE,
                    flag=-9999,
                    n_cores=args.n_cores,
                    loglevel=args.logging_level,
                    logfile=args.log_file,
                )

    if args.presolve == 1:
        # write modtran presolve template
        write_modtran_template(
            atmosphere_type=args.atmosphere_type,
            fid=paths.fid,
            altitude_km=mean_altitude_km,
            dayofyear=dayofyear,
            to_sensor_zenith=mean_to_sensor_zenith,
            to_sun_zenith=mean_to_sun_zenith,
            to_sun_azimuth=mean_to_sun_azimuth,
            relative_azimuth=mean_relative_azimuth,
            gmtime=gmtime,
            elevation_km=mean_elevation_km,
            output_file=paths.h2o_template_path,
            ihaze_type="AER_NONE",
        )

        if args.emulator_base is None:
            max_water = calc_modtran_max_water(paths)
        else:
            max_water = 6

        # run H2O grid as necessary
        if not exists(envi_header(paths.h2o_subs_path)) or not exists(
            paths.h2o_subs_path
        ):
            # Write the presolve connfiguration file
            h2o_grid = np.linspace(0.01, max_water - 0.01, 10).round(2)
            logging.info(f"Pre-solve H2O grid: {h2o_grid}")
            logging.info("Writing H2O pre-solve configuration file.")
            build_presolve_config(
                paths=paths,
                h2o_lut_grid=h2o_grid,
                n_cores=args.n_cores,
                use_emp_line=use_superpixels,
                surface_category=args.surface_category,
                emulator_base=args.emulator_base,
                uncorrelated_radiometric_uncertainty=uncorrelated_radiometric_uncertainty,
            )

            # Run modtran retrieval
            logging.info("Run ISOFIT initial guess")
            retrieval_h2o = isofit.Isofit(
                paths.h2o_config_path, level="INFO", logfile=args.log_file
            )
            retrieval_h2o.run()
            del retrieval_h2o

            # clean up unneeded storage
            if args.emulator_base is None:
                for to_rm in [
                    "*r_k",
                    "*t_k",
                    "*tp7",
                    "*wrn",
                    "*psc",
                    "*plt",
                    "*7sc",
                    "*acd",
                ]:
                    cmd = "rm " + join(paths.lut_h2o_directory, to_rm)
                    logging.info(cmd)
                    os.system(cmd)
        else:
            logging.info("Existing h2o-presolve solutions found, using those.")

        h2o = envi.open(envi_header(paths.h2o_subs_path))
        h2o_est = h2o.read_band(-1)[:].flatten()

        p05 = np.percentile(h2o_est[h2o_est > lut_params.h2o_min], 2)
        p95 = np.percentile(h2o_est[h2o_est > lut_params.h2o_min], 98)
        margin = (p95 - p05) * 0.5

        lut_params.h2o_range[0] = max(lut_params.h2o_min, p05 - margin)
        lut_params.h2o_range[1] = min(max_water, max(lut_params.h2o_min, p95 + margin))

    h2o_lut_grid = lut_params.get_grid(
        lut_params.h2o_range[0],
        lut_params.h2o_range[1],
        lut_params.h2o_spacing,
        lut_params.h2o_spacing_min,
    )

    logging.info("Full (non-aerosol) LUTs:")
    logging.info(f"Elevation: {elevation_lut_grid}")
    logging.info(f"To-sensor zenith: {to_sensor_zenith_lut_grid}")
    logging.info(f"To-sun zenith: {to_sun_zenith_lut_grid}")
    logging.info(f"Relative to-sun azimuth: {relative_azimuth_lut_grid}")
    logging.info(f"H2O Vapor: {h2o_lut_grid}")

    logging.info(paths.state_subs_path)
    if (
        not exists(paths.state_subs_path)
        or not exists(paths.uncert_subs_path)
        or not exists(paths.rfl_subs_path)
    ):
        write_modtran_template(
            atmosphere_type=args.atmosphere_type,
            fid=paths.fid,
            altitude_km=mean_altitude_km,
            dayofyear=dayofyear,
            to_sensor_zenith=mean_to_sensor_zenith,
            to_sun_zenith=mean_to_sun_zenith,
            to_sun_azimuth=mean_to_sun_azimuth,
            relative_azimuth=mean_relative_azimuth,
            gmtime=gmtime,
            elevation_km=mean_elevation_km,
            output_file=paths.modtran_template_path,
        )

        logging.info("Writing main configuration file.")
        build_main_config(
            paths=paths,
            lut_params=lut_params,
            h2o_lut_grid=h2o_lut_grid,
            elevation_lut_grid=(
                elevation_lut_grid
                if elevation_lut_grid is not None
                else [mean_altitude_km]
            ),
            to_sensor_zenith_lut_grid=(
                to_sensor_zenith_lut_grid
                if to_sensor_zenith_lut_grid is not None
                else [mean_to_sensor_zenith]
            ),
            to_sun_zenith_lut_grid=(
                to_sun_zenith_lut_grid
                if to_sun_zenith_lut_grid is not None
                else [mean_to_sun_zenith]
            ),
            relative_azimuth_lut_grid=(
                relative_azimuth_lut_grid
                if relative_azimuth_lut_grid is not None
                else [mean_relative_azimuth]
            ),
            mean_latitude=mean_latitude,
            mean_longitude=mean_longitude,
            dt=dt,
            use_emp_line=use_superpixels,
            n_cores=args.n_cores,
            surface_category=args.surface_category,
            emulator_base=args.emulator_base,
            uncorrelated_radiometric_uncertainty=uncorrelated_radiometric_uncertainty,
            multiple_restarts=args.multiple_restarts,
            segmentation_size=args.segmentation_size,
            pressure_elevation=args.pressure_elevation,
        )

        # Run modtran retrieval
        logging.info("Running ISOFIT with full LUT")
        retrieval_full = isofit.Isofit(
            paths.modtran_config_path, level="INFO", logfile=args.log_file
        )
        retrieval_full.run()
        del retrieval_full

        # clean up unneeded storage
        if args.emulator_base is None:
            for to_rm in [
                "*r_k",
                "*t_k",
                "*tp7",
                "*wrn",
                "*psc",
                "*plt",
                "*7sc",
                "*acd",
            ]:
                cmd = "rm " + join(paths.lut_modtran_directory, to_rm)
                logging.info(cmd)
                os.system(cmd)

    if not exists(paths.rfl_working_path) or not exists(paths.uncert_working_path):
        # Determine the number of neighbors to use.  Provides backwards stability and works
        # well with defaults, but is arbitrary
        if not args.num_neighbors:
            nneighbors = [int(round(3950 / 9 - 35 / 36 * args.segmentation_size))]
        else:
            nneighbors = [n for n in args.num_neighbors]

        if args.empirical_line == 1:
            # Empirical line
            logging.info("Empirical line inference")
            empirical_line(
                reference_radiance_file=paths.rdn_subs_path,
                reference_reflectance_file=paths.rfl_subs_path,
                reference_uncertainty_file=paths.uncert_subs_path,
                reference_locations_file=paths.loc_subs_path,
                segmentation_file=paths.lbl_working_path,
                input_radiance_file=paths.radiance_working_path,
                input_locations_file=paths.loc_working_path,
                output_reflectance_file=paths.rfl_working_path,
                output_uncertainty_file=paths.uncert_working_path,
                isofit_config=paths.modtran_config_path,
                nneighbors=nneighbors[0],
            )
        elif args.analytical_line == 1:
            logging.info("Analytical line inference")
            analytical_line(
                paths.radiance_working_path,
                paths.loc_working_path,
                paths.obs_working_path,
                args.working_directory,
                output_rfl_file=paths.rfl_working_path,
                output_unc_file=paths.uncert_working_path,
                loglevel=args.logging_level,
                logfile=args.log_file,
                n_atm_neighbors=nneighbors,
                smoothing_sigma=args.atm_sigma,
            )

    logging.info("Done.")


class Pathnames:
    """Class to determine and hold the large number of relative and absolute paths that are needed for isofit and
    MODTRAN configuration files.

    Args:
        args: an argparse Namespace object with all inputs
    """

    def __init__(self, args):
        # Determine FID based on sensor name
        if args.sensor == "ang":
            self.fid = split(args.input_radiance)[-1][:18]
        elif args.sensor == "av3":
            self.fid = split(args.input_radiance)[-1][:18]
        elif args.sensor == "avcl":
            self.fid = split(args.input_radiance)[-1][:16]
        elif args.sensor == "emit":
            self.fid = split(args.input_radiance)[-1][:19]
        elif args.sensor == "enmap":
            self.fid = args.input_radiance.split("/")[-1].split("_")[5]
        elif args.sensor == "hyp":
            self.fid = split(args.input_radiance)[-1][:22]
        elif args.sensor == "neon":
            self.fid = split(args.input_radiance)[-1][:21]
        elif args.sensor == "prism":
            self.fid = split(args.input_radiance)[-1][:18]
        elif args.sensor == "prisma":
            self.fid = args.input_radiance.split("/")[-1].split("_")[1]
        elif args.sensor[:3] == "NA-":
            self.fid = os.path.splitext(os.path.basename(args.input_radiance))[0]

        logging.info("Flightline ID: %s" % self.fid)

        # Names from inputs
        self.aerosol_climatology = args.aerosol_climatology_path
        self.input_radiance_file = args.input_radiance
        self.input_loc_file = args.input_loc
        self.input_obs_file = args.input_obs
        self.working_directory = abspath(args.working_directory)

        self.lut_modtran_directory = abspath(join(self.working_directory, "lut_full/"))

        if args.surface_path:
            self.surface_path = args.surface_path
        else:
            self.surface_path = os.getenv("ISOFIT_SURFACE_MODEL")
        if self.surface_path is None:
            logging.info("No surface model defined")

        # set up some sub-directories
        self.lut_h2o_directory = abspath(join(self.working_directory, "lut_h2o/"))
        self.config_directory = abspath(join(self.working_directory, "config/"))
        self.data_directory = abspath(join(self.working_directory, "data/"))
        self.input_data_directory = abspath(join(self.working_directory, "input/"))
        self.output_directory = abspath(join(self.working_directory, "output/"))

        # define all output names
        rdn_fname = self.fid + "_rdn"
        self.rfl_working_path = abspath(
            join(self.output_directory, rdn_fname.replace("_rdn", "_rfl"))
        )
        self.uncert_working_path = abspath(
            join(self.output_directory, rdn_fname.replace("_rdn", "_uncert"))
        )
        self.lbl_working_path = abspath(
            join(self.output_directory, rdn_fname.replace("_rdn", "_lbl"))
        )
        self.state_working_path = abspath(
            join(self.output_directory, rdn_fname.replace("_rdn", "_state"))
        )
        self.surface_working_path = abspath(join(self.data_directory, "surface.mat"))

        if args.copy_input_files is True:
            self.radiance_working_path = abspath(
                join(self.input_data_directory, rdn_fname)
            )
            self.obs_working_path = abspath(
                join(self.input_data_directory, self.fid + "_obs")
            )
            self.loc_working_path = abspath(
                join(self.input_data_directory, self.fid + "_loc")
            )
        else:
            self.radiance_working_path = abspath(self.input_radiance_file)
            self.obs_working_path = abspath(self.input_obs_file)
            self.loc_working_path = abspath(self.input_loc_file)

        if args.channelized_uncertainty_path:
            self.input_channelized_uncertainty_path = args.channelized_uncertainty_path
        else:
            self.input_channelized_uncertainty_path = os.getenv(
                "ISOFIT_CHANNELIZED_UNCERTAINTY"
            )

        self.channelized_uncertainty_working_path = abspath(
            join(self.data_directory, "channelized_uncertainty.txt")
        )

        if args.model_discrepancy_path:
            self.input_model_discrepancy_path = args.model_discrepancy_path
        else:
            self.input_model_discrepancy_path = None

        self.model_discrepancy_working_path = abspath(
            join(self.data_directory, "model_discrepancy.mat")
        )

        self.rdn_subs_path = abspath(
            join(self.input_data_directory, self.fid + "_subs_rdn")
        )
        self.obs_subs_path = abspath(
            join(self.input_data_directory, self.fid + "_subs_obs")
        )
        self.loc_subs_path = abspath(
            join(self.input_data_directory, self.fid + "_subs_loc")
        )
        self.rfl_subs_path = abspath(
            join(self.output_directory, self.fid + "_subs_rfl")
        )
        self.atm_coeff_path = abspath(
            join(self.output_directory, self.fid + "_subs_atm")
        )
        self.state_subs_path = abspath(
            join(self.output_directory, self.fid + "_subs_state")
        )
        self.uncert_subs_path = abspath(
            join(self.output_directory, self.fid + "_subs_uncert")
        )
        self.h2o_subs_path = abspath(
            join(self.output_directory, self.fid + "_subs_h2o")
        )

        self.wavelength_path = abspath(join(self.data_directory, "wavelengths.txt"))

        self.modtran_template_path = abspath(
            join(self.config_directory, self.fid + "_modtran_tpl.json")
        )
        self.h2o_template_path = abspath(
            join(self.config_directory, self.fid + "_h2o_tpl.json")
        )

        self.modtran_config_path = abspath(
            join(self.config_directory, self.fid + "_modtran.json")
        )
        self.h2o_config_path = abspath(
            join(self.config_directory, self.fid + "_h2o.json")
        )

        if args.modtran_path:
            self.modtran_path = args.modtran_path
        else:
            self.modtran_path = os.getenv("MODTRAN_DIR")

        self.sixs_path = os.getenv("SIXS_DIR")

        if os.getenv("ISOFIT_DIR"):
            self.isofit_path = os.getenv("ISOFIT_DIR")
        else:
            # isofit file should live at isofit/isofit/core/isofit.py
            self.isofit_path = os.path.dirname(
                os.path.dirname(os.path.dirname(isofit.__file__))
            )

        if args.sensor == "avcl":
            self.noise_path = join(self.isofit_path, "data", "avirisc_noise.txt")
        elif args.sensor == "emit":
            self.noise_path = join(self.isofit_path, "data", "emit_noise.txt")
            if self.input_channelized_uncertainty_path is None:
                self.input_channelized_uncertainty_path = join(
                    self.isofit_path, "data", "emit_osf_uncertainty.txt"
                )
            if self.input_model_discrepancy_path is None:
                self.input_model_discrepancy_path = join(
                    self.isofit_path, "data", "emit_model_discrepancy.mat"
                )
        elif args.sensor == "av3":
            self.noise_path = None
            logging.info("no noise path found, proceeding without")
            if self.input_channelized_uncertainty_path is None:
                self.input_channelized_uncertainty_path = join(
                    self.isofit_path, "data", "av3_osf_uncertainty.txt"
                )
        else:
            self.noise_path = None
            logging.info("no noise path found, proceeding without")
            # quit()

        self.earth_sun_distance_path = abspath(
            join(self.isofit_path, "data", "earth_sun_distance.txt")
        )
        self.irradiance_file = abspath(
            join(
                self.isofit_path,
                "examples",
                "20151026_SantaMonica",
                "data",
                "prism_optimized_irr.dat",
            )
        )

        self.aerosol_tpl_path = join(self.isofit_path, "data", "aerosol_template.json")
        self.rdn_factors_path = None
        if args.rdn_factors_path is not None:
            self.rdn_factors_path = abspath(args.rdn_factors_path)

        self.ray_temp_dir = args.ray_temp_dir

    def make_directories(self):
        """Build required subdirectories inside working_directory"""
        for dpath in [
            self.working_directory,
            self.lut_h2o_directory,
            self.lut_modtran_directory,
            self.config_directory,
            self.data_directory,
            self.input_data_directory,
            self.output_directory,
        ]:
            if not exists(dpath):
                os.mkdir(dpath)

    def stage_files(self):
        """Stage data files by copying into working directory"""
        files_to_stage = [
            (self.input_radiance_file, self.radiance_working_path, True),
            (self.input_obs_file, self.obs_working_path, True),
            (self.input_loc_file, self.loc_working_path, True),
            (self.surface_path, self.surface_working_path, False),
            (
                self.input_channelized_uncertainty_path,
                self.channelized_uncertainty_working_path,
                False,
            ),
            (
                self.input_model_discrepancy_path,
                self.model_discrepancy_working_path,
                False,
            ),
        ]

        for src, dst, hasheader in files_to_stage:
            if src is None:
                continue
            if not exists(dst):
                logging.info("Staging %s to %s" % (src, dst))
                copyfile(src, dst)
                if hasheader:
                    copyfile(envi_header(src), envi_header(dst))


class SerialEncoder(json.JSONEncoder):
    """Encoder for json to help ensure json objects can be passed to the workflow manager."""

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        else:
            return super(SerialEncoder, self).default(obj)


class LUTConfig:
    """A look up table class, containing default grid options.  All properties may be overridden with the optional
        input configuration file path

    Args:
        lut_config_file: configuration file to override default values
    """

    def __init__(self, lut_config_file: str = None):
        if lut_config_file is not None:
            with open(lut_config_file, "r") as f:
                lut_config = json.load(f)

        # For each element, set the look up table spacing (lut_spacing) as the
        # anticipated spacing value, or 0 to use a single point (not LUT).
        # Set the 'lut_spacing_min' as the minimum distance allowed - if separation
        # does not meet this threshold based on the available data, on a single
        # point will be used.

        # Units of kilometers
        self.elevation_spacing = 0.25
        self.elevation_spacing_min = 0.2

        # Units of g / m2
        self.h2o_spacing = 0.25
        self.h2o_spacing_min = 0.03

        # Special parameter to specify the minimum allowable water vapor value in g / m2
        self.h2o_min = 0.05

        # Set defaults, will override based on settings
        # Units of g / m2
        self.h2o_range = [0.05, 5]

        # Units of degrees
        self.to_sensor_zenith_spacing = 10
        self.to_sensor_zenith_spacing_min = 2

        # Units of degrees
        self.to_sun_zenith_spacing = 10
        self.to_sun_zenith_spacing_min = 2

        # Units of degrees
        self.relative_azimuth_spacing = 60
        self.relative_azimuth_spacing_min = 25

        # Units of AOD
        self.aerosol_0_spacing = 0
        self.aerosol_0_spacing_min = 0

        # Units of AOD
        self.aerosol_1_spacing = 0
        self.aerosol_1_spacing_min = 0

        # Units of AOD
        self.aerosol_2_spacing = 0.25
        self.aerosol_2_spacing_min = 0

        # Units of AOD
        self.aerosol_0_range = [0.001, 1]
        self.aerosol_1_range = [0.001, 1]
        self.aerosol_2_range = [0.001, 1]
        self.aot_550_range = [0.001, 1]

        self.aot_550_spacing = 0
        self.aot_550_spacing_min = 0
        
        self.rte_auto_rebuild = True
        self.flag_ocean_elevation = False

        # overwrite anything that comes in from the config file
        if lut_config_file is not None:
            for key in lut_config:
                if key in self.__dict__:
                    setattr(self, key, lut_config[key])

    def get_grid(
        self, minval: float, maxval: float, spacing: float, min_spacing: float
    ):
        if spacing == 0:
            logging.debug("Grid spacing set at 0, using no grid.")
            return None
        num_gridpoints = int(np.ceil((maxval - minval) / spacing)) + 1

        grid = np.linspace(minval, maxval, num_gridpoints)

        if min_spacing > 0.0001:
            grid = np.round(grid, 4)
        if len(grid) == 1:
            logging.debug(
                f"Grid spacing is 0, which is less than {min_spacing}.  No grid used"
            )
            return None
        elif np.abs(grid[1] - grid[0]) < min_spacing:
            logging.debug(
                f"Grid spacing is {grid[1]-grid[0]}, which is less than {min_spacing}. "
                " No grid used"
            )
            return None
        else:
            return grid

    def get_angular_grid(
        self,
        angle_data_input: np.array,
        spacing: float,
        min_spacing: float,
        units: str = "d",
    ):
        """Find either angular data 'center points' (num_points = 1), or a lut set that spans
        angle variation in a systematic fashion.

        Args:
            angle_data_input: set of angle data to use to find center points
            spacing: the desired angular spacing between points, or mean if -1
            min_spacing: the minimum angular spacing between points allowed (if less, no grid)
            units: specifies if data are in degrees (default) or radians

        :Returns:
            angular data center point or lut set spanning space

        """
        if spacing == 0:
            logging.debug("Grid spacing set at 0, using no grid.")
            return None

        # Convert everything to radians so we don't have to track throughout
        if units == "r":
            angle_data = np.rad2deg(angle_data_input)
        else:
            angle_data = angle_data_input.copy()

        spatial_data = np.hstack(
            [
                np.cos(np.deg2rad(angle_data)).reshape(-1, 1),
                np.sin(np.deg2rad(angle_data)).reshape(-1, 1),
            ]
        )

        # find which quadrants have data
        quadrants = np.zeros((2, 2))
        if np.any(np.logical_and(spatial_data[:, 0] > 0, spatial_data[:, 1] > 0)):
            quadrants[1, 0] = 1
        if np.any(np.logical_and(spatial_data[:, 0] > 0, spatial_data[:, 1] < 0)):
            quadrants[1, 1] += 1
        if np.any(np.logical_and(spatial_data[:, 0] < 0, spatial_data[:, 1] > 0)):
            quadrants[0, 0] += 1
        if np.any(np.logical_and(spatial_data[:, 0] < 0, spatial_data[:, 1] < 0)):
            quadrants[0, 1] += 1

        # Handle the case where angles are < 180 degrees apart
        if np.sum(quadrants) < 3 and spacing != -1:
            if np.sum(quadrants[1, :]) == 2:
                # If angles cross the 0-degree line:
                angle_spread = self.get_grid(
                    np.min(angle_data + 180),
                    np.max(angle_data + 180),
                    spacing,
                    min_spacing,
                )
                if angle_spread is None:
                    return None
                else:
                    return angle_spread - 180
            else:
                # Otherwise, just space things out:
                return self.get_grid(
                    np.min(angle_data), np.max(angle_data), spacing, min_spacing
                )
        else:
            if spacing >= 180:
                logging.warning(
                    f"Requested angle spacing is {spacing}, but obs angle divergence is"
                    " > 180.  Tighter  spacing recommended"
                )

            # If we're greater than 180 degree spread, there's no universal answer. Try GMM.
            if spacing == -1:
                num_points = 1
            else:
                # This very well might overly space the grid, but we don't / can't know in general
                num_points = int(np.ceil(360 / spacing))

            # We initialize the GMM with a static seed for repeatability across runs
            gmm = mixture.GaussianMixture(
                n_components=num_points, covariance_type="full", random_state=1
            )
            if spatial_data.shape[0] == 1:
                spatial_data = np.vstack([spatial_data, spatial_data])

            # Protect memory against huge images
            if spatial_data.shape[0] > 1e6:
                use = np.linspace(0, spatial_data.shape[0] - 1, int(1e6), dtype=int)
                spatial_data = spatial_data[use, :]

            gmm.fit(spatial_data)
            central_angles = np.degrees(np.arctan2(gmm.means_[:, 1], gmm.means_[:, 0]))
            if num_points == 1:
                return central_angles[0]

            ca_quadrants = np.zeros((2, 2))
            if np.any(np.logical_and(gmm.means_[:, 0] > 0, gmm.means_[:, 1] > 0)):
                ca_quadrants[1, 0] = 1
            elif np.any(np.logical_and(gmm.means_[:, 0] > 0, gmm.means_[:, 1] < 0)):
                ca_quadrants[1, 1] += 1
            elif np.any(np.logical_and(gmm.means_[:, 0] < 0, gmm.means_[:, 1] > 0)):
                ca_quadrants[0, 0] += 1
            elif np.any(np.logical_and(gmm.means_[:, 0] < 0, gmm.means_[:, 1] < 0)):
                ca_quadrants[0, 1] += 1

            if np.sum(ca_quadrants) < np.sum(quadrants):
                logging.warning(
                    f"GMM angles {central_angles} span"
                    f" {np.sum(ca_quadrants)} quadrants, while data spans"
                    f" {np.sum(ca_quadrants)} quadrants"
                )

            return central_angles


def load_climatology(
    config_path: str,
    latitude: float,
    longitude: float,
    acquisition_datetime: datetime,
    isofit_path: str,
    lut_params: LUTConfig,
) -> (np.array, np.array, np.array):
    """Load climatology data, based on location and configuration

    Args:
        config_path: path to the base configuration directory for isofit
        latitude: latitude to set for the segment (mean of acquisition suggested)
        longitude: latitude to set for the segment (mean of acquisition suggested)
        acquisition_datetime: datetime to use for the segment( mean of acquisition suggested)
        isofit_path: base path to isofit installation (needed for data path references)
        lut_params: parameters to use to define lut grid

    :Returns
        tuple containing:
            aerosol_state_vector - A dictionary that defines the aerosol state vectors for isofit
            aerosol_lut_grid - A dictionary of the aerosol lookup table (lut) grid to be explored
            aerosol_model_path - A path to the location of the aerosol model to use with MODTRAN.

    """

    aerosol_model_path = join(isofit_path, "data", "aerosol_model.txt")
    aerosol_state_vector = {}
    aerosol_lut_grid = {}
    aerosol_lut_ranges = [
        lut_params.aerosol_0_range,
        lut_params.aerosol_1_range,
        lut_params.aerosol_2_range,
    ]
    aerosol_lut_spacing = [
        lut_params.aerosol_0_spacing,
        lut_params.aerosol_1_spacing,
        lut_params.aerosol_2_spacing,
    ]
    aerosol_lut_spacing_mins = [
        lut_params.aerosol_0_spacing_min,
        lut_params.aerosol_1_spacing_min,
        lut_params.aerosol_2_spacing_min,
    ]
    for _a, alr in enumerate(aerosol_lut_ranges):
        aerosol_lut = lut_params.get_grid(
            alr[0], alr[1], aerosol_lut_spacing[_a], aerosol_lut_spacing_mins[_a]
        )
        if aerosol_lut is not None:
            aerosol_state_vector["AERFRAC_{}".format(_a)] = {
                "bounds": [float(alr[0]), float(alr[1])],
                "scale": 1,
                "init": float((alr[1] - alr[0]) / 10.0 + alr[0]),
                "prior_sigma": 10.0,
                "prior_mean": float((alr[1] - alr[0]) / 10.0 + alr[0]),
            }

            aerosol_lut_grid["AERFRAC_{}".format(_a)] = aerosol_lut.tolist()

    aot_550_lut = lut_params.get_grid(
        lut_params.aot_550_range[0],
        lut_params.aot_550_range[1],
        lut_params.aot_550_spacing,
        lut_params.aot_550_spacing_min,
    )
    if aot_550_lut is not None:
        aerosol_lut_grid["AOT550"] = aot_550_lut.tolist()
        alr = [aerosol_lut_grid["AOT550"][0], aerosol_lut_grid["AOT550"][-1]]
        aerosol_state_vector["AOT550"] = {
            "bounds": [float(alr[0]), float(alr[1])],
            "scale": 1,
            "init": float((alr[1] - alr[0]) / 10.0 + alr[0]),
            "prior_sigma": 10.0,
            "prior_mean": float((alr[1] - alr[0]) / 10.0 + alr[0]),
        }

    logging.info("Loading Climatology")
    # If a configuration path has been provided, use it to get relevant info
    if config_path is not None:
        month = acquisition_datetime.timetuple().tm_mon
        year = acquisition_datetime.timetuple().tm_year
        with open(config_path, "r") as fin:
            for case in json.load(fin)["cases"]:
                match = True
                logging.info("matching", latitude, longitude, month, year)
                for criterion, interval in case["criteria"].items():
                    logging.info(criterion, interval, "...")
                    if criterion == "latitude":
                        if latitude < interval[0] or latitude > interval[1]:
                            match = False
                    if criterion == "longitude":
                        if longitude < interval[0] or longitude > interval[1]:
                            match = False
                    if criterion == "month":
                        if month < interval[0] or month > interval[1]:
                            match = False
                    if criterion == "year":
                        if year < interval[0] or year > interval[1]:
                            match = False

                if match:
                    aerosol_state_vector = case["aerosol_state_vector"]
                    aerosol_lut_grid = case["aerosol_lut_grid"]
                    aerosol_model_path = case["aerosol_mdl_path"]
                    break

    logging.info(
        "Climatology Loaded.  Aerosol State Vector:\n{}\nAerosol LUT Grid:\n{}\nAerosol"
        " model path:{}".format(
            aerosol_state_vector, aerosol_lut_grid, aerosol_model_path
        )
    )
    return aerosol_state_vector, aerosol_lut_grid, aerosol_model_path


def calc_modtran_max_water(paths: Pathnames) -> float:
    """MODTRAN may put a ceiling on "legal" H2O concentrations.  This function calculates that ceiling.  The intended
     use is to make sure the LUT does not contain useless gridpoints above it.

    Args:
        paths: object containing references to all relevant file locations

    Returns:
        max_water - maximum MODTRAN H2OSTR value for provided obs conditions
    """

    max_water = None
    # TODO: this is effectively redundant from the radiative_transfer->modtran. Either devise a way
    # to port in from there, or put in utils to reduce redundancy.
    xdir = {"linux": "linux", "darwin": "macos", "windows": "windows"}
    name = "H2O_bound_test"
    filebase = os.path.join(paths.lut_h2o_directory, name)
    with open(paths.h2o_template_path, "r") as f:
        bound_test_config = json.load(f)

    bound_test_config["MODTRAN"][0]["MODTRANINPUT"]["NAME"] = name
    bound_test_config["MODTRAN"][0]["MODTRANINPUT"]["ATMOSPHERE"]["H2OSTR"] = 50
    with open(filebase + ".json", "w") as fout:
        fout.write(
            json.dumps(bound_test_config, cls=SerialEncoder, indent=4, sort_keys=True)
        )

    cmd = os.path.join(
        paths.modtran_path, "bin", xdir[platform], "mod6c_cons " + filebase + ".json"
    )
    try:
        subprocess.call(cmd, shell=True, timeout=10, cwd=paths.lut_h2o_directory)
    except:
        pass

    with open(filebase + ".tp6", errors="ignore") as tp6file:
        for count, line in enumerate(tp6file):
            if "The water column is being set to the maximum" in line:
                max_water = line.split(",")[1].strip()
                max_water = float(max_water.split(" ")[0])
                break

    if max_water is None:
        logging.error(
            "Could not find MODTRAN H2O upper bound in file {}".format(
                filebase + ".tp6"
            )
        )
        raise KeyError("Could not find MODTRAN H2O upper bound")

    return max_water


def get_metadata_from_obs(
    obs_file: str,
    lut_params: LUTConfig,
    trim_lines: int = 5,
    max_flight_duration_h: int = 8,
    nodata_value: float = -9999,
) -> (List, bool, float, float, float, np.array, List, List):
    """Get metadata needed for complete runs from the observation file
    (bands: path length, to-sensor azimuth, to-sensor zenith, to-sun azimuth,
    to-sun zenith, phase, slope, aspect, cosine i, UTC time).

    Args:
        obs_file:              file name to pull data from
        lut_params:            parameters to use to define lut grid
        trim_lines:            number of lines to ignore at beginning and end of file (good if lines contain values
                               that are erroneous but not nodata
        max_flight_duration_h: maximum length of the current acquisition, used to check if we've lapped a UTC day
        nodata_value:          value to ignore from location file

    :Returns:
        tuple containing:
            h_m_s - list of the mean-time hour, minute, and second within the line
            increment_day - indicator of whether the UTC day has been changed since the beginning of the line time
            mean_path_km - mean distance between sensor and ground in km for good data
            mean_to_sensor_azimuth - mean to-sensor azimuth for good data
            mean_to_sensor_zenith - mean to-sensor zenith for good data
            mean_to_sun_zenith - mean to-sun zenith for good data
            mean_relative_azimuth - mean relative to-sun azimuth for good data
            valid - boolean array indicating which pixels were NOT nodata
            to_sensor_zenith_lut_grid - the to-sensor zenith look up table grid for good data
            to_sun_zenith_lut_grid - the to-sun zenith look up table grid for good data
            relative_azimuth_lut_grid - the relative to-sun azimuth look up table grid for good data
    """
    obs_dataset = envi.open(envi_header(obs_file), obs_file)
    obs = obs_dataset.open_memmap(interleave="bip", writable=False)
    valid = np.logical_not(np.any(np.isclose(obs, nodata_value), axis=2))

    path_km = obs[:, :, 0] / 1000.0
    to_sensor_azimuth = obs[:, :, 1]
    to_sensor_zenith = obs[:, :, 2]
    to_sun_azimuth = obs[:, :, 3]
    to_sun_zenith = obs[:, :, 4]
    time = obs[:, :, 9]

    # calculate relative to-sun azimuth
    delta_phi = to_sun_azimuth - to_sensor_azimuth
    delta_phi = delta_phi % 360
    relative_azimuth = np.abs(delta_phi - 180)

    use_trim = trim_lines != 0 and valid.shape[0] > trim_lines * 2
    if use_trim:
        actual_valid = valid.copy()
        valid[:trim_lines, :] = False
        valid[-trim_lines:, :] = False

    mean_path_km = np.mean(path_km[valid])
    del path_km

    mean_to_sensor_zenith = 180 - lut_params.get_angular_grid(
        to_sensor_zenith[valid], -1, 0
    )
    mean_to_sun_zenith = lut_params.get_angular_grid(to_sun_zenith[valid], -1, 0)
    mean_to_sun_azimuth = (
        lut_params.get_angular_grid(to_sun_azimuth[valid], -1, 0) % 360
    )
    mean_relative_azimuth = (
        lut_params.get_angular_grid(relative_azimuth[valid], -1, 0) % 360
    )

    # geom_margin = EPS * 2.0
    to_sensor_zenith_lut_grid = lut_params.get_angular_grid(
        to_sensor_zenith[valid],
        lut_params.to_sensor_zenith_spacing,
        lut_params.to_sensor_zenith_spacing_min,
    )
    if to_sensor_zenith_lut_grid is not None:
        to_sensor_zenith_lut_grid = np.sort(180 - to_sensor_zenith_lut_grid)

    to_sun_zenith_lut_grid = lut_params.get_angular_grid(
        to_sun_zenith[valid],
        lut_params.to_sun_zenith_spacing,
        lut_params.to_sun_zenith_spacing_min,
    )
    if to_sun_zenith_lut_grid is not None:
        to_sun_zenith_lut_grid = np.sort(to_sun_zenith_lut_grid)

    relative_azimuth_lut_grid = lut_params.get_angular_grid(
        relative_azimuth[valid],
        lut_params.relative_azimuth_spacing,
        lut_params.relative_azimuth_spacing_min,
    )
    if relative_azimuth_lut_grid is not None:
        relative_azimuth_lut_grid = np.sort(
            np.array([x % 360 for x in relative_azimuth_lut_grid])
        )

    del to_sensor_azimuth
    del to_sensor_zenith
    del to_sun_azimuth
    del to_sun_zenith
    del relative_azimuth

    # Make time calculations
    mean_time = np.mean(time[valid])
    min_time = np.min(time[valid])
    max_time = np.max(time[valid])

    increment_day = False
    # UTC day crossover corner case
    if max_time > 24 - max_flight_duration_h and min_time < max_flight_duration_h:
        time[np.logical_and(time < max_flight_duration_h, valid)] += 24
        mean_time = np.mean(time[valid])

        # This means the majority of the line was really in the next UTC day,
        # increment the line accordingly
        if mean_time > 24:
            mean_time -= 24
            increment_day = True

    # Calculate hour, minute, second
    h_m_s = [np.floor(mean_time)]
    h_m_s.append(np.floor((mean_time - h_m_s[-1]) * 60))
    h_m_s.append(np.floor((mean_time - h_m_s[-2] - h_m_s[-1] / 60.0) * 3600))

    if use_trim:
        valid = actual_valid

    return (
        h_m_s,
        increment_day,
        mean_path_km,
        mean_to_sensor_zenith,
        mean_to_sun_zenith,
        mean_to_sun_azimuth,
        mean_relative_azimuth,
        valid,
        to_sensor_zenith_lut_grid,
        to_sun_zenith_lut_grid,
        relative_azimuth_lut_grid,
    )


def get_metadata_from_loc(
    loc_file: str,
    lut_params: LUTConfig,
    trim_lines: int = 5,
    nodata_value: float = -9999,
    pressure_elevation: bool = False,
) -> (float, float, float, np.array):
    """Get metadata needed for complete runs from the location file (bands long, lat, elev).

    Args:
        loc_file: file name to pull data from
        lut_params: parameters to use to define lut grid
        trim_lines: number of lines to ignore at beginning and end of file (good if lines contain values that are
                    erroneous but not nodata
        nodata_value: value to ignore from location file
        pressure_elevation: retrieve pressure elevation (requires expanded ranges)

    :Returns:
        tuple containing:
            mean_latitude - mean latitude of good values from the location file
            mean_longitude - mean latitude of good values from the location file
            mean_elevation_km - mean ground estimate of good values from the location file
            elevation_lut_grid - the elevation look up table, based on globals and values from location file
    """

    loc_dataset = envi.open(envi_header(loc_file), loc_file)
    loc_data = loc_dataset.open_memmap(interleave="bsq", writable=False)
    valid = np.logical_not(np.any(loc_data == nodata_value, axis=0))

    use_trim = trim_lines != 0 and valid.shape[0] > trim_lines * 2
    if use_trim:
        valid[:trim_lines, :] = False
        valid[-trim_lines:, :] = False

    # Grab zensor position and orientation information
    mean_latitude = lut_params.get_angular_grid(loc_data[1, valid].flatten(), -1, 0)
    mean_longitude = lut_params.get_angular_grid(
        -1 * loc_data[0, valid].flatten(), -1, 0
    )

    mean_elevation_km = np.mean(loc_data[2, valid]) / 1000.0

    # make elevation grid
    min_elev = np.min(loc_data[2, valid]) / 1000.0
    max_elev = np.max(loc_data[2, valid]) / 1000.0
    if pressure_elevation:
        min_elev = max(min_elev - 2, 0.25)
        max_elev += 2
    elevation_lut_grid = lut_params.get_grid(
        min_elev,
        max_elev,
        lut_params.elevation_spacing,
        lut_params.elevation_spacing_min,
    )

    return mean_latitude, mean_longitude, mean_elevation_km, elevation_lut_grid


def build_presolve_config(
    paths: Pathnames,
    h2o_lut_grid: np.array,
    n_cores: int = -1,
    use_emp_line: bool = False,
    surface_category="multicomponent_surface",
    emulator_base: str = None,
    uncorrelated_radiometric_uncertainty: float = 0.0,
    segmentation_size: int = 400,
    debug: bool = False,
) -> None:
    """Write an isofit config file for a presolve, with limited info.

    Args:
        paths: object containing references to all relevant file locations
        h2o_lut_grid: the water vapor look up table grid isofit should use for this solve
        n_cores: number of cores to use in processing
        use_emp_line: flag whether or not to set up for the empirical line estimation
        surface_category: type of surface to use
        emulator_base: the basename of the emulator, if used
        uncorrelated_radiometric_uncertainty: uncorrelated radiometric uncertainty parameter for isofit
        segmentation_size: image segmentation size if empirical line is used
        debug: flag to enable debug_mode in the config.implementation
    """

    # Determine number of spectra included in each retrieval.  If we are
    # operating on segments, this will average down instrument noise
    if use_emp_line:
        spectra_per_inversion = segmentation_size
    else:
        spectra_per_inversion = 1

    if emulator_base is None:
        engine_name = "modtran"
    else:
        engine_name = "sRTMnet"

    if surface_category == "glint_model_surface":
        glint_model = True
        multipart_transmittance = True
    else:
        glint_model = False
        multipart_transmittance = False

    radiative_transfer_config = {
        "glint_model": glint_model,
        "radiative_transfer_engines": {
            "vswir": {
                "engine_name": engine_name,
                "multipart_transmittance": multipart_transmittance,
                "lut_path": paths.lut_h2o_directory,
                "template_file": paths.h2o_template_path,
                "lut_names": ["H2OSTR"],
                "statevector_names": ["H2OSTR"],
            }
        },
        "statevector": {
            "H2OSTR": {
                "bounds": [float(np.min(h2o_lut_grid)), float(np.max(h2o_lut_grid))],
                "scale": 0.01,
                "init": np.percentile(h2o_lut_grid, 25),
                "prior_sigma": 100.0,
                "prior_mean": 1.5,
            }
        },
        "lut_grid": {
            "H2OSTR": [float(x) for x in h2o_lut_grid],
        },
        "unknowns": {"H2O_ABSCO": 0.0},
    }

    if emulator_base is not None:
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "emulator_file"
        ] = abspath(emulator_base)
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "emulator_aux_file"
        ] = abspath(os.path.splitext(emulator_base)[0] + "_aux.npz")
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "interpolator_base_path"
        ] = abspath(
            os.path.join(
                paths.lut_h2o_directory, os.path.basename(emulator_base) + "_vi"
            )
        )
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "earth_sun_distance_file"
        ] = paths.earth_sun_distance_path
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "irradiance_file"
        ] = paths.irradiance_file
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "engine_base_dir"
        ] = paths.sixs_path

    else:
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "engine_base_dir"
        ] = paths.modtran_path

    # make isofit configuration
    isofit_config_h2o = {
        "ISOFIT_base": paths.isofit_path,
        "output": {"estimated_state_file": paths.h2o_subs_path},
        "input": {},
        "forward_model": {
            "instrument": {
                "wavelength_file": paths.wavelength_path,
                "integrations": spectra_per_inversion,
                "unknowns": {
                    "uncorrelated_radiometric_uncertainty": uncorrelated_radiometric_uncertainty
                },
            },
            "surface": {
                "surface_category": surface_category,
                "surface_file": paths.surface_working_path,
                "select_on_init": True,
            },
            "radiative_transfer": radiative_transfer_config,
        },
        "implementation": {
            "ray_temp_dir": paths.ray_temp_dir,
            "inversion": {"windows": INVERSION_WINDOWS},
            "n_cores": n_cores,
            "debug_mode": debug,
        },
    }

    if paths.input_channelized_uncertainty_path is not None:
        isofit_config_h2o["forward_model"]["instrument"]["unknowns"][
            "channelized_radiometric_uncertainty_file"
        ] = paths.channelized_uncertainty_working_path

    if paths.input_model_discrepancy_path is not None:
        isofit_config_h2o["forward_model"][
            "model_discrepancy_file"
        ] = paths.model_discrepancy_working_path

    if paths.noise_path is not None:
        isofit_config_h2o["forward_model"]["instrument"][
            "parametric_noise_file"
        ] = paths.noise_path
    else:
        isofit_config_h2o["forward_model"]["instrument"]["SNR"] = 1000

    if paths.rdn_factors_path:
        isofit_config_h2o["input"][
            "radiometry_correction_file"
        ] = paths.rdn_factors_path

    if use_emp_line:
        isofit_config_h2o["input"]["measured_radiance_file"] = paths.rdn_subs_path
        isofit_config_h2o["input"]["loc_file"] = paths.loc_subs_path
        isofit_config_h2o["input"]["obs_file"] = paths.obs_subs_path
    else:
        isofit_config_h2o["input"][
            "measured_radiance_file"
        ] = paths.radiance_working_path
        isofit_config_h2o["input"]["loc_file"] = paths.loc_working_path
        isofit_config_h2o["input"]["obs_file"] = paths.obs_working_path

    # write modtran_template
    with open(paths.h2o_config_path, "w") as fout:
        fout.write(
            json.dumps(isofit_config_h2o, cls=SerialEncoder, indent=4, sort_keys=True)
        )


def build_main_config(
    paths: Pathnames,
    lut_params: LUTConfig,
    h2o_lut_grid: np.array = None,
    elevation_lut_grid: np.array = None,
    to_sensor_zenith_lut_grid: np.array = None,
    to_sun_zenith_lut_grid: np.array = None,
    relative_azimuth_lut_grid: np.array = None,
    mean_latitude: float = None,
    mean_longitude: float = None,
    dt: datetime = None,
    use_emp_line: bool = True,
    n_cores: int = -1,
    surface_category="multicomponent_surface",
    emulator_base: str = None,
    uncorrelated_radiometric_uncertainty: float = 0.0,
    multiple_restarts: bool = False,
    segmentation_size=400,
    pressure_elevation: bool = False,
    debug: bool = False,
) -> None:
    """Write an isofit config file for the main solve, using the specified pathnames and all given info

    Args:
        paths:                                object containing references to all relevant file locations
        lut_params:                           configuration parameters for the lut grid
        h2o_lut_grid:                         the water vapor look up table grid isofit should use for this solve
        elevation_lut_grid:                   the ground elevation look up table grid isofit should use for this solve
        to_sensor_zenith_lut_grid:            the to-sensor zenith angle look up table grid isofit should use for this
                                              solve
        to_sun_zenith_lut_grid:               the to-sun zenith angle look up table grid isofit should use for this
                                              solve
        relative_azimuth_lut_grid:            the relative to-sun azimuth angle look up table grid isofit should use for
                                              this solve
        mean_latitude:                        the latitude isofit should use for this solve
        mean_longitude:                       the longitude isofit should use for this solve
        dt:                                   the datetime object corresponding to this flightline to use for this solve
        use_emp_line:                         flag whether or not to set up for the empirical line estimation
        n_cores:                              the number of cores to use during processing
        surface_category:                     type of surface to use
        emulator_base:                        the basename of the emulator, if used
        uncorrelated_radiometric_uncertainty: uncorrelated radiometric uncertainty parameter for isofit
        multiple_restarts:                    if true, use multiple restarts
        segmentation_size:                    image segmentation size if empirical line is used
        pressure_elevation:                   if true, retrieve pressure elevation
        debug:                                if true, run ISOFIT in debug mode
    """

    # Determine number of spectra included in each retrieval.  If we are
    # operating on segments, this will average down instrument noise
    if use_emp_line:
        spectra_per_inversion = segmentation_size
    else:
        spectra_per_inversion = 1

    if emulator_base is None:
        engine_name = "modtran"
    else:
        engine_name = "sRTMnet"

    if surface_category == "glint_model_surface":
        glint_model = True
        multipart_transmittance = True
    else:
        glint_model = False
        multipart_transmittance = False

    radiative_transfer_config = {
        "glint_model": glint_model,
        "radiative_transfer_engines": {
            "vswir": {
                "engine_name": engine_name,
                "multipart_transmittance": multipart_transmittance,
                "lut_path": paths.lut_modtran_directory,
                "aerosol_template_file": paths.aerosol_tpl_path,
                "template_file": paths.modtran_template_path,
                # lut_names - populated below
                # statevector_names - populated below
            }
        },
        "statevector": {},
        "lut_grid": {},
        "unknowns": {"H2O_ABSCO": 0.0},
    }

    if h2o_lut_grid is not None:
        radiative_transfer_config["statevector"]["H2OSTR"] = {
            "bounds": [h2o_lut_grid[0], h2o_lut_grid[-1]],
            "scale": 1,
            "init": (h2o_lut_grid[1] + h2o_lut_grid[-1]) / 2.0,
            "prior_sigma": 100.0,
            "prior_mean": (h2o_lut_grid[1] + h2o_lut_grid[-1]) / 2.0,
        }

    if pressure_elevation:
        radiative_transfer_config["statevector"]["GNDALT"] = {
            "bounds": [elevation_lut_grid[0], elevation_lut_grid[-1]],
            "scale": 100,
            "init": (elevation_lut_grid[1] + elevation_lut_grid[-1]) / 2.0,
            "prior_sigma": 1000.0,
            "prior_mean": (elevation_lut_grid[1] + elevation_lut_grid[-1]) / 2.0,
        }

    if emulator_base is not None:
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "emulator_file"
        ] = abspath(emulator_base)
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "emulator_aux_file"
        ] = abspath(os.path.splitext(emulator_base)[0] + "_aux.npz")
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "interpolator_base_path"
        ] = abspath(
            os.path.join(
                paths.lut_modtran_directory,
                os.path.basename(os.path.splitext(emulator_base)[0]) + "_vi",
            )
        )
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "earth_sun_distance_file"
        ] = paths.earth_sun_distance_path
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "irradiance_file"
        ] = paths.irradiance_file
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "engine_base_dir"
        ] = paths.sixs_path

    else:
        radiative_transfer_config["radiative_transfer_engines"]["vswir"][
            "engine_base_dir"
        ] = paths.modtran_path

    # add aerosol elements from climatology
    aerosol_state_vector, aerosol_lut_grid, aerosol_model_path = load_climatology(
        paths.aerosol_climatology,
        mean_latitude,
        mean_longitude,
        dt,
        paths.isofit_path,
        lut_params=lut_params,
    )
    radiative_transfer_config["statevector"].update(aerosol_state_vector)
    radiative_transfer_config["lut_grid"].update(aerosol_lut_grid)
    radiative_transfer_config["radiative_transfer_engines"]["vswir"][
        "aerosol_model_file"
    ] = aerosol_model_path

    if h2o_lut_grid is not None:
        radiative_transfer_config["lut_grid"]["H2OSTR"] = h2o_lut_grid.tolist()
    if elevation_lut_grid is not None and len(elevation_lut_grid) > 1:
        radiative_transfer_config["lut_grid"][
            "surface_elevation_km"
        ] = elevation_lut_grid.tolist()
    if to_sensor_zenith_lut_grid is not None and len(to_sensor_zenith_lut_grid) > 1:
        radiative_transfer_config["lut_grid"][
            "observer_zenith"
        ] = to_sensor_zenith_lut_grid.tolist()  # modtran convention
    if to_sun_zenith_lut_grid is not None and len(to_sun_zenith_lut_grid) > 1:
        radiative_transfer_config["lut_grid"][
            "solar_zenith"
        ] = to_sun_zenith_lut_grid.tolist()
    if relative_azimuth_lut_grid is not None and len(relative_azimuth_lut_grid) > 1:
        radiative_transfer_config["lut_grid"][
            "relative_azimuth"
        ] = relative_azimuth_lut_grid.tolist()

    # MODTRAN should know about our whole LUT grid and all of our statevectors, so copy them in
    radiative_transfer_config["radiative_transfer_engines"]["vswir"][
        "statevector_names"
    ] = list(radiative_transfer_config["statevector"].keys())
    radiative_transfer_config["radiative_transfer_engines"]["vswir"][
        "lut_names"
    ] = list(radiative_transfer_config["lut_grid"].keys())

    # make isofit configuration
    isofit_config_modtran = {
        "ISOFIT_base": paths.isofit_path,
        "input": {},
        "output": {},
        "forward_model": {
            "instrument": {
                "wavelength_file": paths.wavelength_path,
                "integrations": spectra_per_inversion,
                "unknowns": {
                    "uncorrelated_radiometric_uncertainty": uncorrelated_radiometric_uncertainty
                },
            },
            "surface": {
                "surface_file": paths.surface_working_path,
                "surface_category": surface_category,
                "select_on_init": True,
            },
            "radiative_transfer": radiative_transfer_config,
        },
        "implementation": {
            "ray_temp_dir": paths.ray_temp_dir,
            "inversion": {"windows": INVERSION_WINDOWS},
            "n_cores": n_cores,
            "debug_mode": debug
        },
    }
    
    if hasattr(lut_params,"rte_auto_rebuild"):
        isofit_config_modtran["implementation"]["rte_auto_rebuild"] = lut_params.rte_auto_rebuild
        logging.info(f"Set rte_auto_rebuild from LUT config file (rte_auto_rebuild = {lut_params.rte_auto_rebuild})")
    else:
        logging.info('rte_auto_rebuild not in lut_params')
    
    if use_emp_line:
        isofit_config_modtran["input"]["measured_radiance_file"] = paths.rdn_subs_path
        isofit_config_modtran["input"]["loc_file"] = paths.loc_subs_path
        isofit_config_modtran["input"]["obs_file"] = paths.obs_subs_path
        isofit_config_modtran["output"]["estimated_state_file"] = paths.state_subs_path
        isofit_config_modtran["output"][
            "posterior_uncertainty_file"
        ] = paths.uncert_subs_path
        isofit_config_modtran["output"][
            "estimated_reflectance_file"
        ] = paths.rfl_subs_path
        isofit_config_modtran["output"][
            "atmospheric_coefficients_file"
        ] = paths.atm_coeff_path
    else:
        isofit_config_modtran["input"][
            "measured_radiance_file"
        ] = paths.radiance_working_path
        isofit_config_modtran["input"]["loc_file"] = paths.loc_working_path
        isofit_config_modtran["input"]["obs_file"] = paths.obs_working_path
        isofit_config_modtran["output"][
            "posterior_uncertainty_file"
        ] = paths.uncert_working_path
        isofit_config_modtran["output"][
            "estimated_reflectance_file"
        ] = paths.rfl_working_path
        isofit_config_modtran["output"][
            "estimated_state_file"
        ] = paths.state_working_path

    if multiple_restarts:
        eps = 1e-2
        grid = {}
        if h2o_lut_grid is not None:
            h2o_delta = float(h2o_lut_grid[-1]) - float(h2o_lut_grid[0])
            grid["H2OSTR"] = [
                round(h2o_lut_grid[0] + h2o_delta * 0.02, 4),
                round(h2o_lut_grid[-1] - h2o_delta * 0.02, 4),
            ]

        # We will initialize using different AODs for the first aerosol in the LUT
        if len(aerosol_lut_grid) > 0:
            key = list(aerosol_lut_grid.keys())[0]
            aer_delta = aerosol_lut_grid[key][-1] - aerosol_lut_grid[key][0]
            grid[key] = [
                round(aerosol_lut_grid[key][0] + aer_delta * 0.02, 4),
                round(aerosol_lut_grid[key][-1] - aer_delta * 0.02, 4),
            ]
        isofit_config_modtran["implementation"]["inversion"]["integration_grid"] = grid
        isofit_config_modtran["implementation"]["inversion"][
            "inversion_grid_as_preseed"
        ] = True

    if paths.input_channelized_uncertainty_path is not None:
        isofit_config_modtran["forward_model"]["instrument"]["unknowns"][
            "channelized_radiometric_uncertainty_file"
        ] = paths.channelized_uncertainty_working_path

    if paths.input_model_discrepancy_path is not None:
        isofit_config_modtran["forward_model"][
            "model_discrepancy_file"
        ] = paths.model_discrepancy_working_path

    if paths.noise_path is not None:
        isofit_config_modtran["forward_model"]["instrument"][
            "parametric_noise_file"
        ] = paths.noise_path
    else:
        isofit_config_modtran["forward_model"]["instrument"]["SNR"] = 500

    if paths.rdn_factors_path:
        isofit_config_modtran["input"][
            "radiometry_correction_file"
        ] = paths.rdn_factors_path

    # write modtran_template
    with open(paths.modtran_config_path, "w") as fout:
        fout.write(
            json.dumps(
                isofit_config_modtran, cls=SerialEncoder, indent=4, sort_keys=True
            )
        )


def write_modtran_template(
    atmosphere_type: str,
    fid: str,
    altitude_km: float,
    dayofyear: int,
    to_sensor_zenith: float,
    to_sun_zenith: float,
    to_sun_azimuth: float,
    relative_azimuth: float,
    gmtime: float,
    elevation_km: float,
    output_file: str,
    ihaze_type: str = "AER_RURAL",
):
    """Write a MODTRAN template file for use by isofit look up tables

    Args:
        atmosphere_type:   label for the type of atmospheric profile to use in modtran
        fid:               flight line id (name)
        altitude_km:       altitude of the sensor in km
        dayofyear:         the current day of the given year
        to_sun_zenith:     final altitude solar zenith angle (0→180°)
        to_sun_azimuth:    solar azimuth angle to the sensor, in degrees (AVIRIS convention)
        to_sensor_zenith:  sensor/observer zenith angle, in degrees (MODTRAN convention: 180 - AVIRIS convention)
        relative_azimuth:  final altitude relative solar azimuth (0→360°)
        gmtime:            greenwich mean time
        elevation_km:      elevation of the land surface in km
        output_file:       location to write the modtran template file to
        ihaze_type:        type of extinction and default meteorological range for the boundary-layer aerosol model
    """
    # make modtran configuration
    h2o_template = {
        "MODTRAN": [
            {
                "MODTRANINPUT": {
                    "NAME": fid,
                    "DESCRIPTION": "",
                    "CASE": 0,
                    "RTOPTIONS": {
                        "MODTRN": "RT_CORRK_FAST",
                        "LYMOLC": False,
                        "T_BEST": False,
                        "IEMSCT": "RT_SOLAR_AND_THERMAL",
                        "IMULT": "RT_DISORT",
                        "DISALB": False,
                        "NSTR": 8,
                        "SOLCON": 0.0,
                    },
                    "ATMOSPHERE": {
                        "MODEL": atmosphere_type,
                        "M1": atmosphere_type,
                        "M2": atmosphere_type,
                        "M3": atmosphere_type,
                        "M4": atmosphere_type,
                        "M5": atmosphere_type,
                        "M6": atmosphere_type,
                        "CO2MX": 410.0,
                        "H2OSTR": 1.0,
                        "H2OUNIT": "g",
                        "O3STR": 0.3,
                        "O3UNIT": "a",
                    },
                    "AEROSOLS": {"IHAZE": ihaze_type},
                    "GEOMETRY": {
                        "ITYPE": 3,
                        "H1ALT": altitude_km,
                        "IDAY": dayofyear,
                        "IPARM": 12,
                        "PARM1": relative_azimuth,
                        "PARM2": to_sun_zenith,
                        "TRUEAZ": to_sun_azimuth,
                        "OBSZEN": to_sensor_zenith,
                        "GMTIME": gmtime,
                    },
                    "SURFACE": {
                        "SURFTYPE": "REFL_LAMBER_MODEL",
                        "GNDALT": elevation_km,
                        "NSURF": 1,
                        "SURFP": {"CSALB": "LAMB_CONST_0_PCT"},
                    },
                    "SPECTRAL": {
                        "V1": 340.0,
                        "V2": 2520.0,
                        "DV": 1,
                        "FWHM": 1,
                        "YFLAG": "R",
                        "XFLAG": "N",
                        "FLAGS": "NT A   ",
                        "BMNAME": "01_2013",
                    },
                    "FILEOPTIONS": {"NOPRNT": 2, "CKPRNT": True},
                }
            }
        ]
    }

    # write modtran_template
    with open(output_file, "w") as fout:
        fout.write(
            json.dumps(h2o_template, cls=SerialEncoder, indent=4, sort_keys=True)
        )


@click.command(name="apply_oe")
@click.argument("input_radiance")
@click.argument("input_loc")
@click.argument("input_obs")
@click.argument("working_directory")
@click.argument("sensor")
@click.option("--copy_input_files", type=int, default=0)
@click.option("--modtran_path")
@click.option("--wavelength_path")
@click.option("--surface_category", default="multicomponent_surface")
@click.option("--aerosol_climatology_path")
@click.option("--rdn_factors_path")
@click.option("--surface_path")
@click.option("--atmosphere_type", default="ATM_MIDLAT_SUMMER")
@click.option("--channelized_uncertainty_path")
@click.option("--model_discrepancy_path")
@click.option("--lut_config_file")
@click.option("--multiple_restarts", is_flag=True, default=False)
@click.option("--logging_level", default="INFO")
@click.option("--log_file")
@click.option("--n_cores", type=int, default=1)
@click.option("--presolve", type=int, default=0)
@click.option("--empirical_line", type=int, default=0)
@click.option("--analytical_line", type=int, default=0)
@click.option("--ray_temp_dir", default="/tmp/ray")
@click.option("--emulator_base")
@click.option("--segmentation_size", default=40)
@click.option("--num_neighbors", "-nn", type=int, multiple=True)
@click.option("--atm_sigma", "-as", type=float, multiple=True, default=[2])
@click.option("--pressure_elevation", is_flag=True, default=False)
@click.option(
    "--debug-args",
    help="Prints the arguments list without executing the command",
    is_flag=True,
)
def _cli(debug_args, **kwargs):
    """\
    Apply OE to a block of data
    """
    if debug_args:
        click.echo("Arguments to be passed:")
        for key, value in kwargs.items():
            click.echo(f"  {key} = {value!r}")
    else:
        # SimpleNamespace converts a dict into dot-notational
        apply_oe(SimpleNamespace(**kwargs))

    click.echo("Done")


if __name__ == "__main__":
    _cli()
else:
    from isofit import cli

    cli.add_command(_cli)
