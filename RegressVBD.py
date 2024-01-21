#! /usr/bin/env python
# -*- python-fmt -*-

## Copyright (c) 2023  University of Washington.
##
## Redistribution and use in source and binary forms, with or without
## modification, are permitted provided that the following conditions are met:
##
## 1. Redistributions of source code must retain the above copyright notice, this
##    list of conditions and the following disclaimer.
##
## 2. Redistributions in binary form must reproduce the above copyright notice,
##    this list of conditions and the following disclaimer in the documentation
##    and/or other materials provided with the distribution.
##
## 3. Neither the name of the University of Washington nor the names of its
##    contributors may be used to endorse or promote products derived from this
##    software without specific prior written permission.
##
## THIS SOFTWARE IS PROVIDED BY THE UNIVERSITY OF WASHINGTON AND CONTRIBUTORS “AS
## IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
## IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
## DISCLAIMED. IN NO EVENT SHALL THE UNIVERSITY OF WASHINGTON OR CONTRIBUTORS BE
## LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
## CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
## GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
## HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
## LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
## OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

""" regress for VBD bias and flight coefficiencts over multiple dives
    using just nc files (does not use log, eng, or sg_calib_constants)
"""

import numpy as np
import CommLog
import os
import scipy.interpolate
import scipy.optimize
import plotly.graph_objects

import BaseOpts
import scipy

import math
import Utils
import sys

def flightModelW(bu, ph, xl, a, b, c, rho, s):
    gravity = 9.81
    tol = 0.001

    w = np.sign(bu) * math.sqrt(abs(bu) / 1000.0) / 10
    th = 3.14159 / 4.0 * np.sign(bu)

    buoyforce = 0.001 * gravity * bu

    q = pow(np.sign(bu) * buoyforce / (xl * xl * b), 1.0 / (1.0 + s))
    alpha = 0

    q_old = 0
    if bu == 0 or np.sign(bu) * np.sign(ph) <= 0:
        return w

    j = 0
    while j < 15 and abs((q - q_old) / q) > tol:
        q_old = q
        if abs(math.tan(th)) < 0.01 or q == 0:
            return w

        param = 4.0 * b * c / (a * a * pow(math.tan(th), 2.0) * pow(q, -s))
        if param > 1:
            return w

        q = (
            buoyforce
            * math.sin(th)
            / (2.0 * xl * xl * b * pow(q, s))
            * (1.0 + math.sqrt(1.0 - param))
        )
        alpha = -a * math.tan(th) / (2.0 * c) * (1.0 - math.sqrt(1.0 - param))

        thdeg = ph - alpha
        th = thdeg * 3.14159 / 180.0

        j = j + 1

    umag = math.sqrt(2.0 * q / rho)
    return umag * math.sin(th)

def w_misfit(x0, W, Vol, Dens, Pit, m, rho, vol0):
    bias = x0[0]
    hd_a = x0[1]
    hd_b = x0[2]
    hd_c = x0[3]

    rms = 0
    for k, vbd in enumerate(Vol): 
        v = vol0*1e6 + vbd - bias
        bu = 1000.0 * (-m + Dens[k] * v * 1.0e-6)
        w = 100*flightModelW(bu, Pit[k], 1.8, hd_a, hd_b, hd_c, rho, -0.25)
        rms = rms + pow(w - W[k], 2.0)

    return math.sqrt(rms/len(Vol))

def getVars(fname, basis_C_VBD, basis_VBD_CNV):
    nc = Utils.open_netcdf_file(fname)

    c_vbd = nc.variables["log_C_VBD"].getValue()

    # SG eng time base
    time  = nc.variables["time"][:]
    depth = nc.variables["depth"][:]
    pitch = nc.variables["eng_pitchAng"][:]
    vbd   = nc.variables["eng_vbdCC"][:] + (c_vbd - basis_C_VBD)*basis_VBD_CNV

    # CTD time base
    ctd_time = nc.variables["ctd_time"][:]
    density_ctd = nc.variables["density"][:]
    inds = np.nonzero(
        np.logical_and.reduce(
            (
                np.isfinite(density_ctd),
            )
        )
    )[0]
    ctd_time = ctd_time[inds]
    density_ctd = density_ctd[inds]

    f = scipy.interpolate.interp1d(
        ctd_time,
        density_ctd,
        kind="linear",
        bounds_error=False,
        fill_value="extrapolate",
    )
    density = f(time)

    w = Utils.ctr_1st_diff(-depth * 100, time)

    nc.close()

    return w, depth, vbd, density, pitch

def getModelW(bu, Pit, HD_A, HD_B, HD_C, rho0):
    W = bu.copy()
    for k, b in enumerate(bu):
        W[k] = 100*flightModelW(b, Pit[k], 1.8, 
                                HD_A, HD_B, HD_C, rho0, -0.25)
    return W

def regress(path, glider, dives, depthlims, init_bias, doplot, plot_dives):

    fname = os.path.join(path, f'p{glider:03d}{dives[-1]:04d}.nc')
    nc = Utils.open_netcdf_file(fname)
    basis_C_VBD = nc.variables["log_C_VBD"].getValue()
    basis_HD_A = nc.variables["log_HD_A"].getValue()
    basis_HD_B = nc.variables["log_HD_B"].getValue()
    basis_HD_C = nc.variables["log_HD_C"].getValue()
    basis_RHO0 = nc.variables["log_RHO"].getValue()*1000
    basis_MASS = nc.variables["log_MASS"].getValue()/1000
    basis_VBD_CNV = nc.variables["log_VBD_CNV"].getValue()
    basis_VBD_MIN = nc.variables["log_VBD_MIN"].getValue()

    vol0 = basis_MASS/basis_RHO0;

    mission = nc.variables["sg_cal_mission_title"][:].tobytes().decode("utf-8")

    nc.close()

    W        = np.empty(shape=(0))
    Dens     = np.empty(shape=(0))
    Vol      = np.empty(shape=(0))
    Pit      = np.empty(shape=(0))

    for i in dives:
        fname = os.path.join(path, f'p{glider:03d}{i:04d}.nc')

        w, depth, vbd, density, pitch = getVars(fname, basis_C_VBD, basis_VBD_CNV)

        inds = np.nonzero(
            np.logical_and.reduce(
                (
                    depth > depthlims[0],
                    depth < depthlims[1],
                    np.isfinite(density)
                )
            )
        )[0]
    
        W    = np.concatenate((W, w[inds]))
        Vol  = np.concatenate((Vol, vbd[inds]))
        Dens = np.concatenate((Dens, density[inds]))
        Pit  = np.concatenate((Pit, pitch[inds]))

    bu = 1000.0 * (-basis_MASS + Dens * (vol0*1e6 + Vol) * 1.0e-6)
    W_unbiased = getModelW(bu, Pit,
                           basis_HD_A, basis_HD_B, basis_HD_C, basis_RHO0)

    rms_init = math.sqrt(np.sum((W_unbiased - W)*(W_unbiased - W))/len(W))

    x0 = [init_bias, basis_HD_A, basis_HD_B, basis_HD_C]
    x, rms_final, iter, calls, warns = scipy.optimize.fmin(func=w_misfit, x0=x0, args=(W, Vol, Dens, Pit, basis_MASS, basis_RHO0, vol0), full_output=True, maxiter=2000, ftol=1e-3)
    bias = x[0]
    hd_a = x[1]
    hd_b = x[2]
    hd_c = x[3]
    volmax = vol0*1e6 + (basis_VBD_MIN - basis_C_VBD)*basis_VBD_CNV - bias
    c_vbd = basis_C_VBD + bias/basis_VBD_CNV

    if not doplot:
        return bias, (hd_a, hd_b, hd_c), (rms_init, rms_final), volmax, c_vbd, None

    bu = 1000.0 * (-basis_MASS + Dens * (vol0*1e6 + Vol - bias) * 1.0e-6)
    W_biased = getModelW(bu, Pit, hd_a, hd_b, hd_c, basis_RHO0)

    minx = min([min(W_biased), min(W_unbiased)])
    maxx = max([max(W_biased), max(W_unbiased)])
    miny = min(W)
    maxy = max(W)

    minlim = min([minx, miny])
    maxlim = max([maxx, maxy])
    lim = max([abs(minlim), abs(maxlim)])*1.05

    fig = plotly.graph_objects.Figure()
    fig.add_trace(
        {
            "x": W_biased,
            "y": W,
            "name": "corrected",
            "type": "scatter",
            "mode": "markers",
            "marker": {
                "symbol": "triangle-down",
                "color": "Red",
            },
            "hovertemplate": "%{x:.0f},%{y:.0f}<br><extra></extra>",
        }
    )
    fig.add_trace(
        {
            "x": W_unbiased,
            "y": W,
            "name": "uncorrected",
            "type": "scatter",
            "mode": "markers",
            "marker": {
                "symbol": "triangle-up",
                "color": "Blue",
            },
            "hovertemplate": "%{x:.0f},%{y:.0f}<br><extra></extra>",
        }
    )
    fig.add_trace(
        {
            "x": [-lim, lim],
            "y": [-lim, lim],
            "mode": "lines",
            "line": {"color": "Black"},
            "showlegend": False
        }
    )

    title = f"SG{glider:03d} {mission} dives {dives} from {depthlims}m <br>Initial RMS={rms_init:.3f} cm/s, Final RMS={rms_final:.3f} cm/s<br>Bias={bias:.2f} cc, Implied volmax={volmax:.1f} cc, C_VBD={c_vbd:.1f}<br>HD_A={hd_a:.5f},HD_B={hd_b:.5f},HD_C={hd_c:.3e}"

    fig.update_layout(
        {
            "xaxis": {
                "title": "model vert vel (cm/s)",
                "showgrid": True,
                "range": [-lim, lim]
            },
            "yaxis": {
                "title": "observed vert vel (cm/s)",
                "showgrid": True,
                "range": [-lim, lim]
            },
            "title": {
                "text": title, 
                "xanchor": "center",
                "yanchor": "top",
                "x": 0.5,
                "y": 0.95,
            },
            "margin": {
                "t": 100,
                "b": 125,
            },
        }
    )

    fig.update_yaxes(
        scaleanchor="x",
        scaleratio=1,
    )

    imgs = None

    if doplot == 'png':
        imgs = [fig.to_image(format="png")]
    elif doplot == 'html':
        imgs = [fig.to_html()]

    if plot_dives:
        for i in dives:
            fname = os.path.join(path, f'p{glider:03d}{i:04d}.nc')

            w, depth, vbd, density, pitch = getVars(fname, basis_C_VBD, basis_VBD_CNV)

            inds = np.nonzero(
                np.logical_and.reduce(
                    (
                        np.isfinite(pitch),
                        np.isfinite(w),
                        np.isfinite(density)
                    )
                )
            )
           
            w       = w[inds]
            depth   = depth[inds]
            vbd     = vbd[inds]
            density = density[inds]
            pitch   = pitch[inds]

            bu = 1000.0 * (-basis_MASS + density * (vol0*1e6 + vbd) * 1.0e-6)
            w_unbiased = getModelW(bu, pitch,
                                   basis_HD_A, basis_HD_B, basis_HD_C, basis_RHO0)

            bu = 1000.0 * (-basis_MASS + density * (vol0*1e6 + vbd - bias) * 1.0e-6)
            w_biased = getModelW(bu, pitch, hd_a, hd_b, hd_c, basis_RHO0)

            inds = np.nonzero(
                np.logical_and.reduce(
                    (
                        np.isfinite(w_unbiased),
                        np.isfinite(w_biased),
                    )
                )
            )

            w       = w[inds]
            depth   = depth[inds]
            w_biased = w_biased[inds]
            w_unbiased = w_unbiased[inds]

            fig = plotly.graph_objects.Figure()
            fig.add_trace(
                {
                    "x": w, 
                    "y": depth,
                    "name": "observed",
                    "mode": "lines",
                    "line": { "color": "black", },
                    "hovertemplate": "%{x:.0f},%{y:.0f}<br><extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "x": w_unbiased,
                    "y": depth,
                    "name": "uncorrected model",
                    "mode": "lines",
                     "line": { "color": "Blue", },
                    "hovertemplate": "%{x:.0f},%{y:.0f}<br><extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "x": w_biased,
                    "y": depth,
                    "name": "corrected",
                    "mode": "lines",
                    "line": {"color": "Red"},
                    "hovertemplate": "%{x:.0f},%{y:.0f}<br><extra></extra>",
                }
            )

            title = f"SG{glider:03d} {mission} dive {i}<br>(correct model (red) closer to observed (black) is better)"

            fig.update_layout(
                {
                    "xaxis": {
                        "title": "vert vel (cm/s)",
                        "showgrid": True,
                    },
                    "yaxis": {
                        "title": "depth (m)",
                        "showgrid": True,
                        "autorange": "reversed",
                    },
                    "title": {
                        "text": title, 
                        "xanchor": "center",
                        "yanchor": "top",
                        "x": 0.5,
                        "y": 0.95,
                    },
                    "margin": {
                        "t": 100,
                        "b": 125,
                    },
                }
            )

            if doplot == 'png':
                imgs.append(fig.to_image(format="png"))
            elif doplot == 'html':
                imgs.append(fig.to_html())

    return bias, (hd_a, hd_b, hd_c), (rms_init, rms_final), volmax, c_vbd, imgs

from itertools import chain

def parseSingleRange(rng):
    parts = rng.split('-')
    if 1 > len(parts) > 2:
        return []

    parts = [int(i) for i in parts]
    start = parts[0]
    end = start if len(parts) == 1 else parts[1]
    if start > end:
        end, start = start, end

    return range(start, end + 1)

def parseRangeList(rngs):
    return sorted(set(chain(*[parseSingleRange(rng) for rng in rngs.split(',')])))

def main():

    base_opts = BaseOpts.BaseOptions("Command line app for VBD regression",
        additional_arguments={
            "dives": BaseOpts.options_t(
                "",
                ("RegressVBD",),
                ( "--dives", ), 
                str,
                {
                    "help": "dives to process (e.g.: 3-4,7)",
                    "required": ("RegressVBD",) 
                }
            ),
            "depths": BaseOpts.options_t(
                "",
                ("RegressVBD",),
                ( "--depths", ), 
                str,
                {
                    "help": "depth limits (e.g.: 40,140)",
                    "required": ("RegressVBD",) 
                }
            ),
            "out": BaseOpts.options_t(
                "",
                ("RegressVBD",),
                ( "--out", ), 
                str,
                {
                    "help": "output file name",
                }
            ),
            "initial_bias": BaseOpts.options_t(
                -50,
                ("RegressVBD",),
                ( "--initial_bias", ),
                float,
                {
                    "help": "initial bias estimate (cc)",
                }
            ),
        }
    )

    if not base_opts.instrument_id:
        (comm_log, _, _, _, _) = CommLog.process_comm_log(
            os.path.join(base_opts.mission_dir, "comm.log"),
            base_opts,
        )
        if comm_log:
            base_opts.instrument_id = comm_log.get_instrument_id()

    if not base_opts.instrument_id:
        _, tail = os.path.split(base_opts.mission_dir[:-1])
        if tail[-5:-3] != "sg":
            print("Can't figure out the instrument id - bailing out")
            return
        try:
            base_opts.instrument_id = int(tail[-3:])
        except:
            print("Can't figure out the instrument id - bailing out")
            return

    depthlims = base_opts.depths.split(',')
    if len(depthlims) != 2:
        print("invalid depth limits")
        return

    try:
        d0 = float(depthlims[0])
        d1 = float(depthlims[1])    
    except:
        print("invalid depth lims")
        return

    dives = parseRangeList(base_opts.dives)
    if not dives or len(dives) < 1:
        print("invalid dives list")
        return 

    if base_opts.out and 'html' in base_opts.out:
        fmt = 'html'
    elif base_opts.out and 'png' in base_opts.out:
        fmt = 'png'
    else:
        fmt = False

    bias, hd, rms, vmax, c, plt = regress(base_opts.mission_dir,
                      base_opts.instrument_id,
                      parseRangeList(base_opts.dives),
                      [d0, d1],
                      base_opts.initial_bias, 
                      fmt, True)

    if fmt == 'html':
        fid = open(base_opts.out, 'w')
        fid.write("<br>".join(plt))
        fid.close()
    elif fmt == 'png':
        for k,p in enumerate(plt):
            fid = open(f"{base_opts.out}{k}", 'wb')
            fid.write(p)
            fid.close()
  
    print(f"Initial RMS = {rms[0]:.3f}")
    print(f"final RMS   = {rms[1]:.3f}")
    print(f"VBD bias    = {bias:.2f} cc")
    print(f"HD a,b,c    = [{hd[0]:.5f},{hd[1]:.5f},{hd[2]:.3e}]")
    print(f"Implied volmax = {vmax:.1f} cc")
    print(f"Implied C_VBD  = {c:.1f}")

if __name__ == "__main__":
    retval = 1

    retval = main()

    sys.exit(retval)

