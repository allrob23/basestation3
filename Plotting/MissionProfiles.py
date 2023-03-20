#! /usr/bin/env python
# -*- python-fmt -*-

##
## Copyright (c) 2022, 2023 by University of Washington.  All rights reserved.
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

""" Plots sections of sensor data
"""
# TODO: This can be removed as of python 3.11
from __future__ import annotations

import typing
import sqlite3

import plotly

import os
import yaml
import BaseDB
import Globals
# pylint: disable=wrong-import-position
if typing.TYPE_CHECKING:
    import BaseOpts

import PlotUtilsPlotly
import Utils

from BaseLog import log_error, log_warning
from Plotting import plotmissionsingle


# DEBUG_PDB = "darwin" in sys.platform
DEBUG_PDB = False

def getValue(x, v, sk, vk, fallback):
    if v in x['sections'][sk]:
        y = x['sections'][sk][v]
    elif v in x['variables'][vk]:
        y = x['variables'][vk][v]
    elif v in x['defaults']:
        y = x['defaults'][v]
    else:
        y = fallback

    return y

@plotmissionsingle
def mission_profiles(
    base_opts: BaseOpts.BaseOptions, mission_str: list, dive=None, generate_plots=True
) -> tuple[list, list]:

    if not generate_plots:
        return ([], [])

    section_file_name = os.path.join(base_opts.mission_dir, "sections.yml")
    if not os.path.exists(section_file_name):
        return ([], [])

    with open(section_file_name, "r") as f:
        x = yaml.safe_load(f.read())

    if not 'variables' in x or len(x['variables']) == 0:
        return ([], [])

    if not 'sections' in x or len(x['sections']) == 0:
        return ([], [])

    conn = Utils.open_mission_database(base_opts)
    if not conn:
        log_error("Could not open mission database")
        return ([], [])

    try:
        cur = conn.cursor() 
        cur.execute("SELECT dive FROM dives ORDER BY dive DESC LIMIT 1;")
        res = cur.fetchone()
        latest = res[0]
        cur.close()
    except Exception as e:
        print(e)
        conn.close()
        return ([], [])

    print(latest)
 
    figs = []
    outs = []

    for vk in list(x['variables'].keys()):

        for sk in list(x['sections'].keys()):

            start = getValue(x, 'start', sk, vk, 1)
            step = getValue(x, 'step',   sk, vk, 1)
            stop = getValue(x, 'stop',   sk, vk, -1)
            top  = getValue(x, 'top',    sk, vk, 0)
            bott = getValue(x, 'bottom', sk, vk, 990)
            binZ = getValue(x, 'bin',    sk, vk, 5)
            flip = getValue(x, 'flip',   sk, vk, False)
            whch = getValue(x, 'which',  sk, vk, 4)

            if stop == -1 or stop >= latest:
                stop = latest
                force = True
            else:
                force = False

            fname = f"sg_{vk}_section_{sk}"
            fullname = os.path.join(base_opts.mission_dir, f"plots/{fname}.webp")
            if os.path.exists(fullname) and not force:
                continue

            fig = plotly.graph_objects.Figure()
 
            d = BaseDB.timeSeriesToProfile(None, vk, whch,
                                           start, stop, step,
                                           top, bott, binZ, 
                                           conn)


            fig.add_trace(plotly.graph_objects.Contour(
                x=d['dive'],
                y=d['depth'],
                z=d[vk],
                contours_coloring='heatmap',
                connectgaps=True,
                contours={
                            "coloring": "heatmap",
                            "showlabels": True,
                            "labelfont": 
                            {
                                "family": "Raleway",
                                "size": 12,
                                "color": "white"
                            }
                         }
                )
            )

            title_text = f"{mission_str}<br>{vk}"
            
            fig.update_layout(
                {
                    "xaxis": {
                        "title": "dive",
                        "showgrid": False,
                        "autorange": "reversed" if flip else True,
                    },
                    "yaxis": {
                        "title": "depth",
                        "showgrid": False,
                        "tickformat": "d",
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
                        "b": 120,
                    },
                },
            )

            figs.append(fig)
            out = PlotUtilsPlotly.write_output_files(
                        base_opts,
                        fname,
                        fig,
                  ),
            outs.append(out)

    conn.close()

    return (
        figs,
        outs,
    )
