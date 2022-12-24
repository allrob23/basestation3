#! /usr/bin/env python
# -*- python-fmt -*-

##
## Copyright (c) 2022 by University of Washington.  All rights reserved.
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

""" Plots mission energy consumption and projections
"""
# TODO: This can be removed as of python 3.11
from __future__ import annotations

import collections
import pdb
import sys
import time
import traceback
import typing

import plotly


import numpy as np
import pandas as pd

# pylint: disable=wrong-import-position
if typing.TYPE_CHECKING:
    import BaseOpts

import PlotUtilsPlotly
import Utils

from BaseLog import log_info, log_error
from Plotting import plotmissionsingle


# DEBUG_PDB = "darwin" in sys.platform
DEBUG_PDB = False

# TODO - Tune up colors
# TODO - test with RevB board
# TODO - fix for split voltage

line_type = collections.namedtuple("line_type", ("dash", "color"))

line_lookup = {
    "VBD_pump": line_type("solid", "magenta"),
    "Pitch_motor": line_type("solid", "green"),
    "Roll_motor": line_type("solid", "red"),
    "Iridium": line_type("solid", "black"),
    "Transponder_ping": line_type("solid", "orange"),
    "GPS": line_type("dash", "green"),
    "Compass": line_type("dash", "magenta"),
    # "RAFOS" :
    # "Transponder" :
    # "Compass2" :
    # "network" :
    "STM32Mainboard": line_type("dash", "black"),
    "SciCon": line_type("solid", "DarkMagenta"),
}

# TODO - paramaterize these
g_p_dives_back = 10
p_reserve_percent = 0.15


def estimate_endurance(dive_col, gauge_col, dive_times):
    """Estimate endurace from normalized remaining battery capacity"""
    print(dive_col)
    p_dives_back = g_p_dives_back if dive_col[-1] >= g_p_dives_back else dive_col[-1]

    m, b = np.polyfit(dive_col[-p_dives_back:], gauge_col[-p_dives_back:], 1)
    log_info(f"m:{m} b:{b}")
    lastdive_num = np.int32((p_reserve_percent - b) / m)
    dives_remaining = lastdive_num - dive_col[-1]
    secs_remaining = dives_remaining * np.mean(dive_times[-p_dives_back:])
    end_date = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs_remaining)
    )
    days_remaining = secs_remaining / (24.0 * 3600.0)

    return (dives_remaining, days_remaining, end_date)


@plotmissionsingle
def mission_energy(
    base_opts: BaseOpts.BaseOptions, mission_str: list
) -> tuple[list, list]:
    """Plots mission energy consumption and projections"""
    log_info("Starting mission_energy")

    conn = Utils.open_mission_database(base_opts)
    if not conn:
        log_error("Could not open mission database")
        return ([], [])

    l_annotations = []

    try:
        # capacity 10V and 24V are normalized battery availability

        fg_df = pd.read_sql_query(
            "SELECT dive,fg_kJ_used_10V,fg_kJ_used_24V,fg_batt_capacity_10V,fg_batt_capacity_24V,fg_ah_used_10V,fg_ah_used_24V,log_FG_AHR_10Vo,log_FG_AHR_24Vo from dives",
            conn,
        ).sort_values("dive")

        batt_df = pd.read_sql_query(
            "SELECT dive,batt_capacity_10V,batt_capacity_24V,batt_Ahr_cap_10V,batt_Ahr_cap_24V,batt_ah_10V,batt_ah_24V,batt_volts_10V,batt_volts_24V,batt_kj_used_10V,batt_kj_used_24V,time_seconds_on_surface,time_seconds_diving from dives",
            conn,
        ).sort_values("dive")

        if batt_df["batt_Ahr_cap_24V"].iloc()[-1] == 0:
            univolt = "10V"
        elif batt_df["batt_Ahr_cap_10V"].iloc()[-1] == 0:
            univolt = "24V"
        else:
            univolt = None

        batt_df["dive_time"] = (
            batt_df["time_seconds_on_surface"] + batt_df["time_seconds_diving"]
        )

        scenario_t = collections.namedtuple(
            "scenario_type", ["type_str", "dive_col", "cap_col", "dive_time"]
        )

        scenarios = []
        if univolt:
            scenarios.append(
                scenario_t(
                    "Modeled Use",
                    batt_df["dive"],
                    batt_df[f"batt_capacity_{univolt}"],
                    batt_df["dive_time"],
                )
            )
            # TODO - comment below
            #
            # scenarios.append(
            #     scenario_t(
            #         "Fuel Gauge",
            #         batt_df["dive"],
            #         fg_df[f"fg_batt_capacity_{univolt}"],
            #         batt_df["dive_time"],
            #     )
            # )

        y_offset = -0.08
        for type_str, dive_col, cap_col, dive_time in scenarios:
            dives_remaining, days_remaining, end_date = estimate_endurance(
                dive_col.to_numpy(),
                cap_col.to_numpy(),
                dive_time.to_numpy(),
            )

            p_dives_back = g_p_dives_back if dive_col.to_numpy()[-1] >= g_p_dives_back else dive_col.to_numpy()[-1]

            y_offset += -0.02
            l_annotations.append(
                {
                    "text": f"Based on {type_str} for the last {p_dives_back} dives: {dives_remaining} dives remaining ({days_remaining:.01f} days at current rate) estimated end date {end_date} ",
                    "showarrow": False,
                    "xref": "paper",
                    "yref": "paper",
                    "x": 0.0,
                    "y": y_offset,
                }
            )

        # TODO Using the polyfit on the normailzed battery capacity for the fuel guage yeilds
        # roughly 10% less dives then next calc (taken directly from the current matlab code)
        used_to_date = (
            fg_df["log_FG_AHR_24Vo"].to_numpy()[-1]
            + fg_df["log_FG_AHR_10Vo"].to_numpy()[-1]
        )
        avg_use = (
            np.sum(fg_df["fg_ah_used_24V"].to_numpy()[-p_dives_back:])
            + np.sum(fg_df["fg_ah_used_10V"].to_numpy()[-p_dives_back:])
        ) / 10.0
        log_info(f"avg_use:{avg_use}")
        batt_cap = max(
            batt_df["batt_Ahr_cap_24V"].to_numpy()[-1],
            batt_df["batt_Ahr_cap_10V"].to_numpy()[-1],
        )
        dives_remaining = (
            batt_cap * (1.0 - p_reserve_percent) - used_to_date
        ) / avg_use
        secs_remaining = (
            dives_remaining * batt_df["dive_time"].to_numpy()[-p_dives_back]
        )
        end_date = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + secs_remaining)
        )
        days_remaining = np.mean(secs_remaining) / (24.0 * 3600.0)
        log_info(
            f"Used to date:{used_to_date:.2f} avg_use:{avg_use:.2f} batt_cap:{batt_cap:.2f} dives_remaining{dives_remaining:.0f}, days_remaining:{days_remaining:.2f}"
        )
        y_offset += -0.02
        l_annotations.append(
            {
                "text": f"Based on Fuel Gauge for the last {p_dives_back} dives: {dives_remaining:.0f} dives remaining ({days_remaining:.01f} days at current rate) estimated end date {end_date} ",
                "showarrow": False,
                "xref": "paper",
                "yref": "paper",
                "x": 0.0,
                "y": y_offset,
            }
        )

        # Find the device and sensor columnns for power consumption
        df = pd.read_sql_query("PRAGMA table_info(dives)", conn)

        device_joule_cols = df[
            np.logical_and(
                df["name"].str.endswith("_joules"), df["name"].str.startswith("device_")
            )
        ]["name"].to_list()

        device_joules_df = pd.read_sql_query(
            f"SELECT dive,{','.join(device_joule_cols)} from dives", conn
        ).sort_values("dive")

        # RevE
        if "device_Fast_joules" in device_joule_cols:
            device_joules_df["device_STM32Mainboard_joules"] = (
                device_joules_df["device_Core_joules"]
                + device_joules_df["device_Fast_joules"]
                + device_joules_df["device_Slow_joules"]
                + device_joules_df["device_LPSleep_joules"]
            )
            device_joules_df.drop(
                columns=[
                    "device_Core_joules",
                    "device_Fast_joules",
                    "device_Slow_joules",
                    "device_LPSleep_joules",
                ],
                inplace=True,
            )

        sensor_joule_cols = df[
            np.logical_and(
                df["name"].str.endswith("_joules"), df["name"].str.startswith("sensor_")
            )
        ]["name"].to_list()

        sensor_joules_df = pd.read_sql_query(
            f"SELECT dive,{','.join(sensor_joule_cols)} from dives", conn
        ).sort_values("dive")

        fig = plotly.graph_objects.Figure()

        for energy_joules_df, energy_tag in (
            (device_joules_df, "device_"),
            (sensor_joules_df, "sensor_"),
        ):
            for energy_col in energy_joules_df.columns.to_list():
                if energy_col.startswith(energy_tag):
                    energy_name = energy_col.removeprefix(energy_tag).removesuffix(
                        "_joules"
                    )
                    # A little convoluted, but if a db column is uninitialized, the database NULL gets
                    # converted to a nan, which is treated as non-zero
                    tmp_j = energy_joules_df[energy_col].to_numpy()
                    if np.count_nonzero(tmp_j[~np.isnan(tmp_j)]) == 0:
                        continue
                    fig.add_trace(
                        {
                            "name": energy_name,
                            "x": energy_joules_df["dive"],
                            "y": energy_joules_df[energy_col] / 1000.0,
                            "yaxis": "y1",
                            "mode": "lines",
                            "line": {
                                "dash": line_lookup[energy_name].dash,
                                "color": line_lookup[energy_name].color,
                                "width": 1,
                            },
                            "hovertemplate": energy_name
                            + "<br>Dive %{x:.0f}<br> Energy used %{y:.2f} kJ<extra></extra>",
                        }
                    )

        if univolt:
            fig.add_trace(
                {
                    "name": "Fuel Gauge",
                    "x": fg_df["dive"],
                    "y": fg_df["fg_kJ_used_24V"] + fg_df["fg_kJ_used_10V"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "DarkBlue"},
                    "hovertemplate": "Fuel Gauge<br>Dive %{x:.0f}<br> Energy used %{y:.2f} kJ<extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "name": "Modeled Use",
                    "x": batt_df["dive"],
                    "y": batt_df["batt_kJ_used_10V"] + batt_df["batt_kJ_used_24V"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "DarkGrey"},
                    "hovertemplate": "Modeled Use<br>Dive %{x:.0f}<br>Energy used %{y:.2f} kJ<extra></extra>",
                }
            )
        else:
            fig.add_trace(
                {
                    "name": "10V Fuel Gauge",
                    "x": fg_df["dive"],
                    "y": fg_df["fg_10V_kJ_used"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "LightBlue"},
                    "hovertemplate": "Fuel Gauge 10V<br>Dive %{x:.0f}<br>Energy used %{y:.2f} kJ<extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "name": "24V Fuel Gauge",
                    "x": fg_df["dive"],
                    "y": fg_df["fg_24V_used"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "DarkBlue"},
                    "hovertemplate": "Fuel Gauge 24V<br>Dive %{x:.0f}<br>Energy used %{y:.2f} kJ<extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "name": "10V Modeled Use",
                    "x": batt_df["dive"],
                    "y": batt_df["batt_kJ_used_10V"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "LightGrey"},
                    "hovertemplate": "Modeled Use 10V<br>Dive %{x:.0f}<br>Energy used %{y:.2f} kJ<extra></extra>",
                }
            )
            fig.add_trace(
                {
                    "name": "Modeled Use 24V",
                    "x": batt_df["dive"],
                    "y": batt_df["batt_kj_used_24V"],
                    "yaxis": "y1",
                    "mode": "lines",
                    "line": {"width": 1, "color": "DarkGrey"},
                    "hovertemplate": "Modeled Use 24V<br>Dive %{x:.0f}<br>Energy Used %{y:.2f} kJ<extra></extra>",
                }
            )

        title_text = f"{mission_str}<br>Energy Consumption"

        fig.update_layout(
            {
                "xaxis": {
                    "title": "Dive Number",
                    "showgrid": True,
                    # "side": "top"
                },
                "yaxis": {
                    "title": "energy (kJ)",
                    "showgrid": True,
                    # Fixed ratio
                    # "scaleanchor": "x",
                    # "scaleratio": (plot_lon_max - plot_lon_min)
                    # / (plot_lat_max - plot_lat_min),
                    # Fixed ratio
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
                    "b": 125 if univolt else 175,
                    # "b": 250,
                },
                "annotations": tuple(l_annotations),
            },
        )
        return (
            [fig],
            PlotUtilsPlotly.write_output_files(
                base_opts,
                "eng_mission_energy",
                fig,
            ),
        )

    except:
        if DEBUG_PDB:
            _, _, traceb = sys.exc_info()
            traceback.print_exc()
            pdb.post_mortem(traceb)
        log_error("Could not fetch needed columns", "exc")
        return ([], [])
