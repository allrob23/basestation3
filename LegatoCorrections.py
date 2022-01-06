#! /usr/bin/env python
# -*- python-fmt -*-
##
## Copyright (c) 2021, 2022 by University of Washington.  All rights reserved.
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
Thermal-iniertia corrections for legato ctd
"""
import pdb

import numpy as np
from scipy import signal
import scipy.interpolate
import seawater
import gsw

from BaseLog import log_debug
import Globals
import QC

# TODO - Retain stand alone option here to corrrect the CTD and update the netCDF file


def interp1(t1, data, t2, assume_sorted=False, extrapolate=False, extend=False):
    """
    Interpolates to find Vq, the values of the
    underlying function V=F(X) at the query points Xq.
    """

    if extend:
        # add 'nearest' data item to the ends of data and t1
        if t2[0] < t1[0]:
            # Copy the first value below the interpolation range
            data = np.append(np.array([data[0]]), data)
            t1 = np.append(np.array([t2[0]]), t1)

        if t2[-1] > t1[-1]:
            # Copy the last value above the interpolation range
            data = np.append(data, np.array([data[-1]]))
            t1 = np.append(t1, np.array([t2[-1]]))

    # fill_value="extrapolate" - not appropriate
    # do like matlab here and fill with nan
    f = scipy.interpolate.interp1d(
        t1,
        data,
        bounds_error=False,
        fill_value="extrapolate" if extrapolate else np.nan,
        assume_sorted=assume_sorted,
    )
    return f(t2)


# TODO: Take in C and T QC flags, and apply to the data set before
# correcting
def legato_correct_ct(
    sg_calib_consts_d,
    legato_time,
    legato_press,
    legato_temp,
    legato_temp_qc,
    legato_conduc,
    legato_conduc_qc,
    legato_conductemp,
):
    """
    Performs thermal iniertia corrections for the legato ctd

    Input:
        legato_time - time of observations in seconds
        legato_press - pressure from the legato instrument
        legato_temp - temperature from the legato instrument
        legato_temp_qc - QC vector for temperature
        legato_conduc - conductivity from the legato instrument
        legato_cond_qc - QC vector for cond
        legato_conductemp - conductivity temp from the legato instrument
    Returns:
        corr_pressure - smoothed pressure signal used in the corrections
        corr_temperature - temperature corrected for time lag and termal error
        corr_temperature_qc - updated qc for the correcte temperature
        corr_salinity - corrected salinity
        corr_salinity_qc - updated qc for the corrected salinty
        corr_conductivity - corrected conductivity
        corr_conductivity_qc - updated qc for the corrected conductivity
        corr_salinity_lag_only - salinity from temperature corrected for time lag only

    """
    # Nominal values
    # time_lag = -0.8
    # ctcoeff = 0
    # tau = 10
    # alpha = 0.08
    log_debug("Legato correction values")
    log_debug(f"time_lag:{sg_calib_consts_d['legato_time_lag']:.1f} secs")
    log_debug(f"alpha:{sg_calib_consts_d['legato_alpha']:.1f}")
    log_debug(f"tau:{sg_calib_consts_d['legato_tau']:.1f}")
    log_debug(f"ctcoeef:{sg_calib_consts_d['legato_ctcoeff']:.1f}")

    # Incoming QC for cond is generally a superset of temp with respect to non-good
    # points.  The approach here is fairly simple - consider "good working set" to be
    # the intersection of good points from C and T and only use those in the corrrections
    # below.  Return vectors without _qc vector simply have NaNs for those points outside
    # the good working set

    good_pts = np.logical_and(
        legato_temp_qc == QC.QC_GOOD, legato_conduc_qc == QC.QC_GOOD
    )

    # Check for any pressure points in the good working set that are nan
    good_pts = np.logical_and(good_pts, np.logical_not(np.isnan(legato_press)))

    # interpolate to 1 Hz for corrections
    sample_rate = 1.0

    reg_time = np.arange(
        legato_time[good_pts][0], legato_time[good_pts][-1], sample_rate
    )

    c1 = interp1(legato_time[good_pts], legato_conduc[good_pts], reg_time)
    t1 = interp1(legato_time[good_pts], legato_temp[good_pts], reg_time)
    p1 = interp1(legato_time[good_pts], legato_press[good_pts], reg_time)
    ct = interp1(legato_time[good_pts], legato_conductemp[good_pts], reg_time)

    # CORRECTIONS

    # pressure: smooth a little bit.
    dz_smooth = 2.0
    NN_tmp = np.round(dz_smooth / np.nanmedian(np.abs(np.diff(p1))))
    NN = np.max(np.array((1.0, NN_tmp))).astype(np.int64)

    # Original - breaks down with half-profiles where the dive starts deep
    # p2 = signal.convolve(p1, np.ones(NN) / NN, mode="same")

    # Extend pressure signal out on both ends, convolve and chop
    # This yeilds better results for half profiles, where the signal doesn't start or stop at 0
    p1_tmp = np.empty((len(p1) + NN))
    p1_tmp[NN // 2 : NN // 2 + len(p1)] = p1
    p1_tmp[: NN // 2] = p1[0]
    p1_tmp[NN // 2 + len(p1) :] = p1[-1]

    p2 = signal.convolve(p1_tmp, np.ones(NN) / NN, mode="same")

    # Extract the middle
    p2 = p2[NN // 2 : NN // 2 + len(p1)]

    # Morison et al, 1994; Lueck and Picklo,
    fn = 1.0 / sample_rate / 2.0
    a = (
        4.0
        * fn
        * sg_calib_consts_d["legato_alpha"]
        * sg_calib_consts_d["legato_tau"]
        / (1.0 + 4.0 * fn * sg_calib_consts_d["legato_tau"])
    )
    b = 1.0 - 2.0 * a / sg_calib_consts_d["legato_alpha"]
    Tt = t1 * 0.0
    for nn in range(2, len(Tt)):
        Tt[nn] = -b * Tt[nn - 1] + a * (t1[nn] - t1[nn - 1])

    # advance the temperature by time lag (nominal - 0.8 sec) and thermal error...
    t2 = interp1(reg_time + sg_calib_consts_d["legato_time_lag"], t1 + Tt, reg_time)

    # only the 0.8 sec (no thermal error)
    t_lag_only = interp1(reg_time + sg_calib_consts_d["legato_time_lag"], t1, reg_time)

    # tau60 correction (nominal ctcoeff value means this is a NOP
    c2 = c1 / (1.0 + sg_calib_consts_d["legato_ctcoeff"] * (ct - t1))

    # correction of conductivity due to pressure (fit relative to sg180 Guam 2019)
    # TODO: Still needs verification from RBR on correctness
    P = np.array([3.3058e-05, 0.0488])
    c2 = c2 - (P[0] * p2 + P[1])

    if Globals.f_use_seawater:
        S = seawater.salt(c2 / (seawater.constants.c3515 / 10.0), t2, p2)
        S_lag_only = seawater.salt(
            c1 / (seawater.constants.c3515 / 10.0), t_lag_only, p2
        )
    else:
        S = gsw.SP_from_C(c2 * 10.0, t2, p2)
        S_lag_only = gsw.SP_from_C(c1 * 10.0, t_lag_only, p2)

    # BACK on the original grid.
    corr_pressure = np.full(len(legato_time), np.nan)
    corr_temperature = np.full(len(legato_time), np.nan)
    corr_salinity = np.full(len(legato_time), np.nan)
    corr_conductivity = np.full(len(legato_time), np.nan)
    corr_salinity_lag_only = np.full(len(legato_time), np.nan)

    corr_pressure[np.squeeze(np.nonzero(good_pts))] = interp1(
        reg_time, p2, legato_time[good_pts], extend=True
    )
    corr_temperature[np.squeeze(np.nonzero(good_pts))] = interp1(
        reg_time, t2, legato_time[good_pts], extend=True
    )
    corr_salinity[np.squeeze(np.nonzero(good_pts))] = interp1(
        reg_time, S, legato_time[good_pts], extend=True
    )
    corr_conductivity[np.squeeze(np.nonzero(good_pts))] = interp1(
        reg_time, c2, legato_time[good_pts], extend=True
    )
    corr_salinity_lag_only[np.squeeze(np.nonzero(good_pts))] = interp1(
        reg_time, S_lag_only, legato_time[good_pts], extend=True
    )

    corr_temperature_qc = QC.initialize_qc(len(legato_time))
    QC.assert_qc(
        QC.QC_BAD,
        corr_temperature_qc,
        np.squeeze(np.nonzero(np.logical_not(good_pts))),
        "Legato Corrections",
    )
    corr_salinity_qc = QC.initialize_qc(len(legato_time))
    QC.assert_qc(
        QC.QC_BAD,
        corr_salinity_qc,
        np.squeeze(np.nonzero(np.logical_not(good_pts))),
        "Legato Corrections",
    )
    corr_conductivity_qc = QC.initialize_qc(len(legato_time))
    QC.assert_qc(
        QC.QC_BAD,
        corr_conductivity_qc,
        np.squeeze(np.nonzero(np.logical_not(good_pts))),
        "Legato Corrections",
    )

    return (
        corr_pressure,
        corr_temperature,
        corr_temperature_qc,
        corr_salinity,
        corr_salinity_qc,
        corr_conductivity,
        corr_conductivity_qc,
        corr_salinity_lag_only,
    )
