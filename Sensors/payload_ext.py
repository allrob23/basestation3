#! /usr/bin/env python
# -*- python-fmt -*-

##
## Copyright (c) 2011, 2012, 2013, 2020, 2021, 2022, 2023, 2024, 2025 by University of Washington.  All rights reserved.
##
## This file contains proprietary information and remains the
## unpublished property of the University of Washington. Use, disclosure,
## or reproduction is prohibited except as permitted by express written
## license agreement with the University of Washington.
##
## THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
## AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
## IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
## ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
## LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
## CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
## SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
## INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
## CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
## ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
## POSSIBILITY OF SUCH DAMAGE.
##

"""
Seabird payload CTD basestation sensor extension
"""

import re
import shutil
import time

import numpy as np

import BaseNetCDF
import Utils
from BaseLog import log_debug, log_error, log_info, log_warning


def init_logger(module_name, init_dict=None):
    """
    init_loggers

    returns:
    -1 - error in processing
     0 - success (data found and processed)
    """

    if init_dict is None:
        log_error("No datafile supplied for init_loggers - version mismatch?")
        return -1

    BaseNetCDF.register_sensor_dim_info(
        BaseNetCDF.nc_gpctd_data_info, "gpctd_data_point", "gpctd_time", True, "gpctd"
    )
    # results are computed in MDP
    init_dict[module_name] = {
        "logger_prefix": "pc",
        "eng_file_reader": eng_file_reader,
        "netcdf_metadata_adds": {
            "gpctd": [
                False,
                "c",
                {
                    "long_name": "underway thermosalinograph",
                    "nodc_name": "thermosalinograph",
                    "make_model": "pumped Seabird SBE41 (and optional pumped SBE43)",
                },
                BaseNetCDF.nc_scalar,
            ],  # always scalar
            "log_PC_RECORDABOVE": [
                False,
                "d",
                {
                    "description": "Depth above above which data is recorded",
                    "units": "meters",
                },
                BaseNetCDF.nc_scalar,
            ],
            "log_PC_PROFILE": [
                False,
                "d",
                {
                    "description": "Which part of the dive to record data for - 0 none, 1 dive, 2 climb, 3 both"
                },
                BaseNetCDF.nc_scalar,
            ],
            "log_PC_XMITPROFILE": [
                False,
                "d",
                {
                    "description": "Which profile to transmit back to the basestation - 0 none, 1 dive, 2 climb, 3 both"
                },
                BaseNetCDF.nc_scalar,
            ],
            "log_PC_UPLOADMAX": [
                False,
                "d",
                {"description": "Max size in bytes to uplaod to the basestation"},
                BaseNetCDF.nc_scalar,
            ],
            "log_PC_STARTS": [False, "d", {}, BaseNetCDF.nc_scalar],
            "log_PC_INTERVAL": [False, "d", {}, BaseNetCDF.nc_scalar],
            "gpctd_time": [
                True,
                "d",
                {
                    "standard_name": "time",
                    "units": "seconds since 1970-1-1 00:00:00",
                    "description": "CTD sample time in GMT epoch format",
                },
                (BaseNetCDF.nc_gpctd_data_info,),
            ],
            "gpctd_pressure": [
                "f",
                "d",
                {
                    "standard_name": "sea_water_pressure",
                    "units": "dbar",
                    "description": "CTD reported pressure",
                },
                (BaseNetCDF.nc_gpctd_data_info,),
            ],
            "gpctd_conductivity": [
                "f",
                "d",
                {
                    "standard_name": "sea_water_electrical_conductivity",
                    "units": "S/m",
                    "description": "CTD reported conductivity",
                },
                (BaseNetCDF.nc_gpctd_data_info,),
            ],
            "gpctd_temperature": [
                "f",
                "d",
                {
                    "standard_name": "sea_water_temperature",
                    "units": "degrees_Celsius",
                    "description": "CTD reported temperature",
                },
                (BaseNetCDF.nc_gpctd_data_info,),
            ],
            "gpctd_oxygen": [
                "f",
                "d",
                {"units": "Hz", "description": "Pumped SBE43 O2 frequency"},
                (BaseNetCDF.nc_gpctd_data_info,),
            ],
        },
    }

    return 1


# pylint: disable=unused-argument
def process_data_files(
    base_opts,
    module_name,
    calib_consts,
    fc,
    processed_logger_eng_files,
    processed_logger_other_files,
):
    """Processes other files

    Returns:
        0 - success
        1 - failure
    """
    if fc.is_down_data() or fc.is_up_data():
        shutil.move(fc.full_filename(), fc.mk_base_datfile_name())
        # Convert from hex to eng units
        try:
            fi = open(fc.mk_base_datfile_name(), "r")
        except Exception:
            log_error(f"Could not open {fc.mk_base_datfile_name()} for conversion")
            return 1
        try:
            fo = open(fc.mk_base_engfile_name(), "w")
        except Exception:
            log_error(f"Could not open {fc.mk_base_engfile_name()} for conversion")
            return 1

        ret_val = 0
        line = 0
        header = 1
        # hex_digit = r""

        pattern_O2 = r"(?P<press>[\dA-Fa-f]{5})(?P<temp>[\dA-Fa-f]{5})(?P<cond>[\dA-Fa-f]{5})(?P<o>[\dA-Fa-f]{5})"
        pattern_No_O2 = (
            r"(?P<press>[\dA-Fa-f]{5})(?P<temp>[\dA-Fa-f]{5})(?P<cond>[\dA-Fa-f]{5})"
        )
        pattern = pattern_No_O2
        n_groups = 3
        line_size = 15
        has_O2 = False

        for l_line in fi.readlines():
            l_line = l_line.rstrip()
            line += 1  # noqa:  SIM113

            values = re.search(pattern, l_line)
            if values and len(values.groupdict()) == n_groups:
                if header:
                    header = 0
                    # First time through - see if we get a hit with the O2 column.  If so, it
                    # has O2
                    values_temp = re.search(pattern_O2, l_line)
                    if values_temp and len(values_temp.groupdict()) == 4:
                        has_O2 = True
                        pattern = pattern_O2
                        n_groups = 4
                        line_size = 20
                        values = values_temp
                        log_info("O2 column detected")
                    if has_O2:
                        fo.write(
                            "%columns: gpctd.Pressure,gpctd.Temp,gpctd.Cond,gpctd.O2\n%data:\n"
                        )
                    else:
                        fo.write(
                            "%columns: gpctd.Pressure,gpctd.Temp,gpctd.Cond\n%data:\n"
                        )

                if len(l_line) > line_size:
                    try:
                        fo.write(f"%{l_line}\n")
                    except Exception:
                        log_error(
                            f"Could not write to {fc.mk_base_engfile_name()}", "exc"
                        )
                        return 1
                else:
                    v = values.groupdict()
                    try:
                        if has_O2:
                            fo.write(
                                "%f %f %f %f\n"
                                % (
                                    float(int(v["press"], 16)) / 100.0 - 10.0,
                                    float(int(v["temp"], 16)) / 10000.0 - 5.0,
                                    float(int(v["cond"], 16)) / 100000.0 - 0.05,
                                    float(int(v["o"], 16)) / 10.0,
                                )
                            )
                        else:
                            fo.write(
                                "%f %f %f\n"
                                % (
                                    float(int(v["press"], 16)) / 100.0 - 10.0,
                                    float(int(v["temp"], 16)) / 10000.0 - 5.0,
                                    float(int(v["cond"], 16)) / 100000.0 - 0.05,
                                )
                            )

                    except OSError:
                        log_error(
                            f"Could not write to {fc.mk_base_engfile_name()}", "exc"
                        )
                        return 1
                    except Exception:
                        log_error(
                            "Error processing line %d from %s"
                            % (line, fc.mk_base_datfile_name())
                        )
                        ret_val = 1
            else:
                try:
                    fo.write(f"%{l_line}\n")
                except Exception:
                    log_error(f"Could not write to {fc.mk_base_engfile_name()}", "exc")
                    return 1

        processed_logger_eng_files.append(fc.mk_base_engfile_name())
        return ret_val
    else:
        # These should be non-existant
        log_error(
            f"Don't know how to deal with payload CTD file ({fc.full_filename()})"
        )
        return 1


def eng_file_reader(eng_files, nc_info_d, calib_consts):
    """Reads the eng files for payload ctd instruments

    Input:
        eng_files - list of eng_file that contain one class of file
        nc_info_d - netcdf dictionary
        calib_consts - calib conts dictionary

    Returns:
        ret_list - list of (variable,data) tuples
        netcdf_dict - dictionary of optional netcdf variable additions

    """
    cast_pattern = r"%cast\s*(?P<cast>[0-9]*)\s(?P<starttime>.*)\ssamples\s(?P<start>\d*?)\sto\s(?P<end>\d*?),\sint\s=\s(?P<interval>\d*?),"
    upload_pattern = r"%S>UC(?P<cast>[0-9]*)"
    # NOTE - order of this list meaningful (gpctd.Time must be first)
    # NOTE - 'gpctd.O2' might be appended to this list below of SBE43 is present
    data_headers = ["gpctd.Time", "gpctd.Pressure", "gpctd.Temp", "gpctd.Cond"]
    # has_O2 = False

    netcdf_dict = {}

    netcdf_map = {
        "gpctd.Time": "gpctd_time",  # NOTE 'gpctd_time' will be both the name of the variable and the dimension name (given order above)
        "gpctd.Pressure": "gpctd_pressure",
        "gpctd.Temp": "gpctd_temperature",
        "gpctd.Cond": "gpctd_conductivity",
        "gpctd.O2": "gpctd_oxygen",
    }

    data_vectors = {}
    for fn in eng_files:
        ef = Utils.read_eng_file(fn["file_name"])
        if not ef:
            log_error(f"Could not read {fn['file_name']} - not using in profile")
            continue

        # Check the profile for integrity and record the start time
        casts = {}
        cast = None
        for l_line in ef["file_header"]:
            v = re.search(cast_pattern, l_line)
            if v:
                vals = v.groupdict()
                casts[int(vals["cast"])] = vals
            v = re.search(upload_pattern, l_line)
            if v:
                cast = int(v.groupdict()["cast"])

        log_debug(f"Casts {list(casts.keys())}")
        if cast not in casts:
            log_error(f"Did not find uploaded cast {cast} in {fn['file_name']}")
            continue

        c = casts[cast]
        starttime = time.mktime(
            time.strptime(c["starttime"].rstrip().lstrip(), "%d %b %Y %H:%M:%S")
        )

        # if "gpctd_start_time_adjust" in calib_consts:
        #    try:
        #        starttime += float(calib_consts["gpctd_start_time_adjust"])
        #    except Exception:
        #        log_error("Failed to update GPCTD start time", alert="BAD_GPCTD_CLOCK")

        reported_samples = int(c["end"]) - int(c["start"]) + 1
        interval = float(c["interval"])
        num_samples = len(ef["data"]["gpctd.Pressure"])

        log_debug("%d:%d" % (reported_samples, num_samples))

        if num_samples != reported_samples:
            log_warning(
                "File %s, cast %d reported %d samples but only %d read"
                % (fn["file_name"], cast, reported_samples, num_samples)
            )

        ## # Currently, there is a bug with the gpctd flushing garbage
        ## # to is CF card at the end of a half profile.  For now, there isn't much
        ## # to be done.  As a hack, the last N observations are chucked
        ## # N is at most 22 samples without O2 and 18 samples with O2
        if len(ef["data"].keys()) > 3:
            chucked_samples = -18
        else:
            chucked_samples = -22
        log_info("Chucked samples = %d" % abs(chucked_samples))

        # Note: this logic assumes that the O2 column appears in both or neither profile
        if "gpcdt.O2" in list(ef["data"].keys()):
            # NOTE due to type in header line from previous version so accept gpcdt.O2 as well as gpctd.O2
            log_debug("Eng file has bad O2 header; fixing")
            ef["data"]["gpctd.O2"] = ef["data"]["gpcdt.O2"]
            data = ef["data"]
            del data["gpcdt.O2"]

        if "gpctd.O2" in list(ef["data"].keys()) and "gpctd.O2" not in data_headers:
            data_headers.append("gpctd.O2")  # only add once

        # Create a time vector
        tv = np.zeros(num_samples, np.float64)
        for i in range(num_samples):
            tv[i] = starttime + i * interval
        ef["data"]["gpctd.Time"] = tv

        log_debug(f"Eng file col headers {list(ef['data'].keys())}")

        data_vectors[cast] = {}
        for h in data_headers:
            data_vectors[cast][h] = ef["data"][h][:chucked_samples]
            # data_vectors[cast][h] = ef['data'][h]

    # print time.strftime("%d %b %Y %H:%M:%S", time.gmtime(data_vectors[1]['gpctd.Time'][0]))
    # print time.strftime("%d %b %Y %H:%M:%S", time.gmtime(data_vectors[2]['gpctd.Time'][0]))

    # Create one long profile, if needed
    ov = {}
    for h in data_headers:
        ov[h] = []

    for c in sorted(data_vectors.keys()):
        for h in data_headers:
            ov[h] = np.concatenate((ov[h], data_vectors[c][h]))

    # print time.strftime("%d %b %Y %H:%M:%S", time.gmtime(ov['gpctd.Time'][0]))

    # Build the return list - list of tuples
    ret_list = []
    for h in data_headers:
        ret_list.append((netcdf_map[h], ov[h]))

    return ret_list, netcdf_dict
