#! /usr/bin/env python
# -*- python-fmt -*-

## Copyright (c) 2023, 2024  University of Washington.
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

""" Plots dive vertical velocty and estimates C_VBD
"""
# TODO: This can be removed as of python 3.11
from __future__ import annotations
import typing

import numpy as np
import os

import scipy.interpolate
import plotly.graph_objects

if typing.TYPE_CHECKING:
    import BaseOpts
    import scipy

import math
import BaseDB
import HydroModel
import MakeDiveProfiles
import PlotUtils
import PlotUtilsPlotly
import Utils
import RegressVBD
import CalibConst

from BaseLog import log_warning, log_error, log_info, log_debug
from Plotting import plotdivesingle

import pdb


# TODO typing.List(plotly.fig)
@plotdivesingle
def plot_vert_vel_new(
    base_opts: BaseOpts.BaseOptions,
    dive_nc_file: scipy.io._netcdf.netcdf_file,
    generate_plots=True,
    dbcon=None,
) -> tuple[list, list]:
    """Plots various measures of vetical velocity and estimates volmax and C_VBD"""

    calfile = os.path.join(
        base_opts.mission_dir, "sg_calib_constants.m"
    )

    if os.path.exists(calfile):
        calConst = CalibConst.getSGCalibrationConstants(calfile, suppress_required_error=True)
    else:
        calConst = {}
     
    regress_dives  = calConst.get('regress_dives', 3);
    regress_top    = calConst.get('regress_top_depth', 0.2);
    regress_bottom = calConst.get('regress_bottom_depth', 0.9);
    regress_init_bias = calConst.get('regress_init_bias', -50);

    log_info("Starting dive_vert_vel")

    # Preliminaries
    try:
        start_time = dive_nc_file.start_time

        dive_num = getattr(dive_nc_file, "dive_number")

        mhead = (
            dive_nc_file.variables["log_MHEAD_RNG_PITCHd_Wd"][:]
            .tobytes()
            .decode("utf-8")
            .split(",")
        )

        ctd_depth = dive_nc_file.variables["ctd_depth"][:]
        density_ctd = dive_nc_file.variables["density"][:]
        inds = np.nonzero(
            np.logical_and.reduce(
                (
                    np.isfinite(density_ctd),
                )
            )
        )[0]
        ctd_depth = ctd_depth[inds]
        density_ctd = density_ctd[inds]
        isurf = np.nonzero(
            np.logical_and.reduce(
                (
                    ctd_depth < 1.5,
                    ctd_depth > 0.5,
                )
            )
        )[0]
        if len(isurf) > 1:
            density_1m = np.nanmean(density_ctd[isurf]) / 1000
            depth_1m = np.nanmean(ctd_depth[isurf])
        else:
            isurf = np.argmin(ctd_depth)
            density_1m = density_ctd[isurf] / 1000
            depth_1m = ctd_depth[isurf]

        # print(density_1m, depth_1m)
    except:
        log_error(
            "Could not fetch needed variables - skipping vertical velocity plot", "exc"
        )
        return ([], [])

    depth_max = np.nanmax(ctd_depth)
    if regress_top < 1:
        d0 = regress_top*depth_max
    else:
        d0 = regress_top

    if regress_bottom < 1:
        d1 = regress_bottom*depth_max
    else:
        d1 = regress_bottom

    w_desired = np.fabs(float(mhead[3]))

    min_bias = 0
    best_hd = None
    fit_fig = None

    bias_bo, hd_bo, vels_bo, rms_bo, log, plt, figs, = RegressVBD.regress(
                            base_opts.mission_dir,
                            base_opts.instrument_id,
                            [dive_num],
                            [d0, d1],
                            regress_init_bias,
                            None,   # use log mass
                            'png',  # need to ask for plotting to get dive plots
                            True,   # need to ask for dive plots to get the w velocity outputs  
                            bias_only=True,
                        )                                                                                   
    
    bias, hd, vels, rms, log, plt, figs = RegressVBD.regress(
                            base_opts.mission_dir,
                            base_opts.instrument_id,
                            [dive_num],
                            [d0, d1],
                            regress_init_bias,
                            None,   # use log mass
                            'png',  # need to ask for plotting to get dive plots
                            True,   # need to ask for dive plots to get the w velocity outputs  
                            bias_only=False
                        )                                                                                   

    if rms[1] > 0:
        min_bias = bias
        best_hd = hd
        fit_fig = figs[0]
    
    rms_mul = (0,0)

    if regress_dives > 1:
        dive1 = dive_num - regress_dives + 1
        if dive1 < 1:
            dive1 = 1
        
        bias_mul, hd_mul, vels_mul, rms_mul, log_mul, plt_mul, figs_mul = RegressVBD.regress(
                                base_opts.mission_dir,
                                base_opts.instrument_id,
                                [*range(dive1, dive_num + 1)],
                                [d0, d1],
                                regress_init_bias,
                                None,
                                'html', # generate the quality of fit/model improvement plot in html form
                                True,   # need to ask for dive plots to get the w velocity outputs (for last dive)
                                bias_only=False,
                                decimate=regress_dives
                            )                                                                                   

        if rms_mul[1] > 0:
            min_bias = bias_mul
            best_hd = hd_mul
            fit_fig = figs_mul[0]


    implied_volmax      = log['VOL0'] - ((log['C_VBD'] + min_bias / log['VBD_CNV']) - log['VBD_MIN'])*log['VBD_CNV']
    implied_cvbd        = log['C_VBD'] + min_bias / log['VBD_CNV']
    implied_max_maxbuoy = -(implied_cvbd - log['VBD_MAX'])*log['VBD_CNV'] * log['RHO']
    implied_max_smcc    = -(implied_cvbd - log['VBD_MIN'])*log['VBD_CNV']
    implied_min_smcc_surf = log['MASS'] * (1 / density_1m - 1 / log['RHO']) + 150

    log_info(
        f"implied_cvbd {implied_cvbd:.0f}, implied_volmax {implied_volmax:.1f}, implied_max_smcc {implied_max_smcc:.1f}, implied_max_maxbuoy {implied_max_maxbuoy:.1f}"
    )
    log_info(
        f"min SM_CC {implied_min_smcc_surf:.1f} based on density {density_1m:.5f} at {depth_1m:.2f}m and 150cc to raise the antenna"
    )

    if dbcon == None:
        conn = Utils.open_mission_database(base_opts)
        log_info("plot_vert_vel db opened")
    else:
        conn = dbcon

#   NOT YET - don't overwrite DB values from trad version
#    BaseDB.addValToDB(
#        base_opts, dive_nc_file.dive_number, "implied_C_VBD", implied_cvbd, con=conn
#    )
#    BaseDB.addValToDB(
#        base_opts, dive_nc_file.dive_number, "implied_volmax", implied_volmax, con=conn
#    )
#    BaseDB.addValToDB(
#        base_opts,
#        dive_nc_file.dive_number,
#        "min_SM_CC",
#        implied_min_smcc_surf,
#        con=conn,
#    )
#    try:
#        BaseDB.addSlopeValToDB(
#            base_opts,
#            dive_nc_file.dive_number,
#            ["implied_volmax", "implied_C_VBD"],
#            con=conn,
#        )
#    except:
#        log_error("Failed to add values to database", "exc")
#
    if dbcon == None:
        try:
            conn.commit()
        except Exception as e:
            conn.rollback()
            log_error(f"Failed commit, DiveVertVelocity {e}", "exc")

        log_info("plot_vert_vel db closed")
        conn.close()

    if not generate_plots:
        return ([], [])

    fig = plotly.graph_objects.Figure()
    if vels_bo:
        fig.add_trace(
            {
                "x": [-w_desired, -w_desired],
                "y": [np.nanmin(vels_bo[0]), np.nanmax(vels_bo[0])],
                "name": "Vert Speed Desired (dive)",
                "mode": "lines",
                "line": {"dash": "dash", "color": "Grey"},
                "hovertemplate": "Vert Speed Desired (dive)",
                "showlegend": False,
            }
        )
        fig.add_trace(
            {
                "x": [w_desired, w_desired],
                "y": [np.nanmin(vels_bo[0]), np.nanmax(vels_bo[0])],
                "name": "Vert Speed Desired (climb)",
                "mode": "lines",
                "line": {"dash": "dash", "color": "Grey"},
                "hovertemplate": "Vert Speed Desired (climb)",
                "showlegend": False,
            }
        )

        fig.add_trace(
            {
                "x": vels_bo[1],
                "y": vels_bo[0],
                "name": "Vert Speed observed",
                "mode": "lines",
                "line": {"dash": "solid", "color": "Black"},
                "hovertemplate": "dz/dt<br>%{x:.2f} cm/sec<br>%{y:.2f} meters<br><extra></extra>",
            }
        )

        fig.add_trace(
            {
                "x": vels_bo[2],
                "y": vels_bo[0],
                "name": f"Uncorrected model (C_VBD={log['C_VBD']:.0f})",
                "mode": "lines",
                "line": {"dash": "solid", "color": "Blue"},
                "hovertemplate": "<br>%{x:.2f} cm/sec<br>%{y:.2f} meters<br><extra></extra>",
            }
        )
    if rms_bo[1] > 0:
        fig.add_trace(
            {
                "x": vels_bo[3],
                "y": vels_bo[0],
                "name": f"Buoyancy only corrected model (bias={bias_bo:.1f})",
                "mode": "lines",
                "line": {"dash": "solid", "color": "salmon"},
                "hovertemplate": "<br>%{x:.2f} cm/sec<br>%{y:.2f} meters<br><extra></extra>",
            }
        )
    if rms[1] > 0:
        fig.add_trace(
            {
                "x": vels[3],
                "y": vels[0],
                "name": f"HDM corrected model (bias={bias:.1f})",
                "mode": "lines",
                "line": {"dash": "solid", "color": "crimson"},
                "hovertemplate": "<br>%{x:.2f} cm/sec<br>%{y:.2f} meters<br><extra></extra>",
            }
        )
    if regress_dives > 1 and rms_mul[1] > 0:
        fig.add_trace(
            {
                "x": vels_mul[3],
                "y": vels_mul[0],
                "name": f"HDM corrected model, multi-dive (bias={bias_mul:.1f})",
                "mode": "lines",
                "line": {"dash": "solid", "color": "Magenta"},
                "hovertemplate": "<br>%{x:.2f} cm/sec<br>%{y:.2f} meters<br><extra></extra>",
            }
        )

    mission_dive_str = PlotUtils.get_mission_dive(dive_nc_file)
    title_text = f"{mission_dive_str}<br>Vertical Velocity vs Depth"
    fit_line = (
        f"Best Fit VBD bias={min_bias:.0f}cc Implies: C_VBD={implied_cvbd:.0f}ad, volmax={implied_volmax:.0f}cc, max MAX_BUOY={implied_max_maxbuoy:.0f}cc<br>"
        f"Max SM_CC={implied_max_smcc:.0f}cc, min SM_CC {implied_min_smcc_surf:.1f} (based on density {density_1m:.5f} at {depth_1m:.2f}m and antenna 150cc)"
    )

    fig.update_layout(
        {
            "xaxis": {
                "title": f"Vertical Velocity (cm/sec)<br>{fit_line}",
                "showgrid": True,
                # "side": "top"
            },
            "yaxis": {
                "title": "Depth (m)",
                "showgrid": True,
                "autorange": "reversed",
            },
            "title": {
                "text": title_text,
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

    return (
        [fig, fit_fig],
        [ PlotUtilsPlotly.write_output_files(
              base_opts,
              "dv%04d_vert_vel_new" % (dive_nc_file.dive_number,),
              fig,
          ),
          PlotUtilsPlotly.write_output_files(
              base_opts,
              "dv%04d_vert_vel_regression" % (dive_nc_file.dive_number,),
              fit_fig,
          ) if fit_fig else None]
    )
