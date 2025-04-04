#! /usr/bin/env python
# -*- python-fmt -*-

## Copyright (c) 2023, 2024, 2025  University of Washington.
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

# fmt: off

"""Routines for extracting profiles from dive timeseries files
"""

import json
import sys
import warnings
from json import JSONEncoder

import numpy
import scipy.interpolate

import Globals
import Utils
from BaseLog import log_error, log_warning


def binData(d, x, bins):
    data_binned = scipy.stats.binned_statistic(d, x, statistic='mean', bins=bins)
    return numpy.transpose(data_binned.statistic)

class NumpyArrayEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, numpy.ndarray):
            return obj.tolist()

        return JSONEncoder.default(self, obj)    
   
def dumps(d):
    return json.dumps(d, cls=NumpyArrayEncoder)
 
def timeSeriesToProfile(var, which, 
                        diveStart, diveStop, diveStride, 
                        binStart, binStop, binSize, ncfilename, extnci=None, x=None):

    if extnci is None:
        try:
            nci = Utils.open_netcdf_file(ncfilename, "r")
        except Exception:
            log_error(f"Unable to open {ncfilename}")
            return (None, None)
    else:
        nci = extnci

    message = {}
    message[var] = []
    message['dive'] = []
    message['which'] = []
    message['avg_time'] = []

    bins = [ *range(binStart, binStop + int(binSize/2), binSize) ]
    dives = range(diveStart, diveStop + 1, diveStride)

    if which == Globals.WhichHalf.both:
        arr = numpy.zeros((len(bins) - 1, len(dives)*2))
    else:
        arr = numpy.zeros((len(bins) - 1, len(dives)))
  
    if x is None:
        x = extractVarTimeDepth(None, var, extnci=nci)

    if x is None:
        if extnci is None:
            nci.close()

        return (None, None)

    nan = numpy.empty((len(bins) - 1, ))
    nan[:] = numpy.nan

    i = 0
    for p in dives:

        idx = numpy.where(nci.variables['dive_number'][:] == p)[0]
        if len(idx) == 0:
            t0 = 0
            t1 = -1
            t2 = -1
        else:
            t0 = nci.variables['start_time'][idx]
            t1 = nci.variables['deepest_sample_time'][idx]
            t2 = nci.variables['end_time'][idx]

        if which in (Globals.WhichHalf.down, Globals.WhichHalf.both):
            ixs = (x['time'] > t0) &(x['time'] < t1)
            message['dive'].append(p + 0.25)
            message['which'].append(1)
            message['avg_time'].append(x['time'][ixs].mean())
            d = None
            if sum(1 for x in ixs if x) > 0:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)

                    d = scipy.stats.binned_statistic(x['depth'][ixs],
                                                     x[var][ixs], statistic=numpy.nanmean, bins=bins).statistic

            if d is not None:
                arr[:,i] = d.T
            else:
                arr[:,i] = nan

            i = i + 1

        if which in (Globals.WhichHalf.up, Globals.WhichHalf.both):
            ixs = (x['time'] > t1) & (x['time'] < t2)
            message['dive'].append(p + 0.5)
            message['which'].append(4)
            message['avg_time'].append(x['time'][ixs].mean())
            d = None
            if sum(1 for x in ixs if x) > 0:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)

                    d = scipy.stats.binned_statistic(x['depth'][ixs],
                                                     x[var][ixs], statistic=numpy.nanmean, bins=bins).statistic

            if d is not None:
                arr[:,i] = d.T
            else:
                arr[:,i] = nan

            i = i + 1

        if which == Globals.WhichHalf.combine:
            ixs = (x['time'] > t0) & (x['time'] < t2)
            message['dive'].append(p + 0.5)
            message['which'].append(4)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                message['avg_time'].append(x['time'][ixs].mean())
            d = None
            if sum(1 for x in ixs if x) > 0:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    d = scipy.stats.binned_statistic(x['depth'][ixs],
                                                     x[var][ixs], statistic=numpy.nanmean, bins=bins).statistic
                
            if d is not None:
                arr[:,i] = d.T
            else:
                arr[:,i] = nan

            i = i + 1

    message['depth'] = bins
    message[var] = arr[:,0:i]

    if extnci is None:
        nci.close()

    return (message, x)

def getVarNames(nc_filename, ext_nc_file=None):

    if ext_nc_file is None:
        try:
            nc_file = Utils.open_netcdf_file(nc_filename, "r")
        except Exception:
            log_error(f"Unable to open {nc_filename}")
            return None
    else:
        nc_file = ext_nc_file

    vars = []

    for k in nc_file.variables:
        if len(nc_file.variables[k].dimensions) and '_data_point' in nc_file.variables[k].dimensions[0] and '_dive_number' not in k:
            vars.append({'var': k, 'dim': nc_file.variables[k].dimensions[0]})
            
    if ext_nc_file is None:
        nc_file.close()

    return vars

def extractVars(nc_filename, varNames, dive1, diveN, extnci=None):
    if extnci is None:
        try:
            nci = Utils.open_netcdf_file(nc_filename, "r")
        except Exception:
            log_error(f"Unable to open {nc_filename}")
            return None
    else:
        nci = extnci

    dmax = max(nci.variables['dive_number'][:])
    if dmax < diveN:
        diveN = dmax

    d1 = numpy.where(nci.variables['dive_number'][:] == dive1)[0]
    dN = numpy.where(nci.variables['dive_number'][:] == diveN)[0]
    t0 = nci.variables['start_time'][d1]
    t2 = nci.variables['end_time'][dN]
    base_t = None
    base_t_len = 0
    base_p = None

    x = {}
    for p in varNames:
        x[p] = {}

        var = nci.variables[p][:]
        dim = nci.variables[p].dimensions[0]

        var_t = []
        if 'time' in varNames[-4:]:
            var_t = var
        else:
            for k in nci.variables:
                if 'time' in k[-4:] and len(nci.variables[k].dimensions) and '_data_point' in nci.variables[k].dimensions[0] and dim == nci.variables[k].dimensions[0]:
                    var_t = nci.variables[k][:]
                    break 

        if len(var_t):
            ixs = (var_t > t0) & (var_t < t2)
            x[p]['t'] = var_t[ixs]
            x[p]['value'] = var[ixs]

        if len(var_t[ixs]) > base_t_len:
            base_t = var_t[ixs]
            base_p = p

    message = {}
    message['epoch'] = base_t.tolist()
    message['time']  = (base_t - base_t[0]).tolist()
    message[base_p] = x[p]['value'].tolist()
    for p in varNames:
        if p == base_p:
            continue

        message[p] = numpy.interp(base_t, x[p]['t'], x[p]['value']).tolist()

    if extnci is None:
        nci.close()

    return message

def extractVarTimeDepth(nc_filename, varname, extnci=None):
    if extnci is None:
        try:
            nci = Utils.open_netcdf_file(nc_filename, "r")
        except Exception:
            log_error(f"Unable to open {nc_filename}")
            return None
    else:
        nci = extnci

    if varname not in nci.variables:
        log_warning(f"{varname} not found")
        if extnci is None:
            nci.close()

        return None

    message = {}
    #var = nci.variables[varname][:]
    dim = nci.variables[varname].dimensions[0]

    if dim == 'ctd_data_point':
        message['depth'] = nci.variables['ctd_depth'][:]
        message['time'] = nci.variables['ctd_time'][:]
        message[varname] = nci.variables[varname][:]

        if extnci is None:
            nci.close()

        return message

    try:
        var_t = None
        for k in nci.variables:
            if 'time' in k[-4:] and len(nci.variables[k].dimensions) and '_data_point' in nci.variables[k].dimensions[0] and dim == nci.variables[k].dimensions[0]:
                var_t = nci.variables[k][:]
                break

        if (var_t is not None) and (len(var_t)):
            f = scipy.interpolate.interp1d(
                nci.variables['ctd_time'][:], nci.variables['ctd_depth'][:], kind="linear", bounds_error=False
            )
            message['depth'] = f(var_t)
            message['time'] = var_t
            message[varname] = nci.variables[varname][:]
        else:
            log_error(f'no time variable found for {varname}({dim})')

    except Exception as e:
        log_error(f"Could not extract variable {varname}, {e}")

    if extnci is None:
        nci.close()

    return message

if __name__ == "__main__":


    # print(getVarNames(sys.argv[1]))

    #msg = timeSeriesToProfile('temperature', 4, 1, 859, 1, 0, 990, 5, 'sg249_NANOOS_Jul-2022_timeseries.nc')
    #out = dumps(msg).encode('utf-8')
    msg = extractVars('sg249_NANOOS_Jul-2022_timeseries.nc', ['temperature'], 10, 10)
    print(msg)
    sys.exit(0)
