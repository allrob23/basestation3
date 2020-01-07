#! /usr/bin/env python

##
## Copyright (c) 2006, 2007, 2011, 2012, 2015, 2017, 2018, 2019, 2020 by University of Washington.  All rights reserved.
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

import sys
import traceback
import os
import logging
import collections
import BaseOpts
from io import StringIO

# CONSIDER make adding code location an option for debugging
# e.g., BaseLogger.opts.log_code_location
# otherwise drop for released code
# However, Geoff reports much after-the-fact debugging for which the linenos are critical.
#                 debug,    info,      warning, error,    critical
_stack_options = ['caller', 'caller', 'caller', 'caller', 'exc'] # default
# _stack_options = ['caller', None,      None,    None,     'exc'] # relative silence
# _stack_options = ['caller', None,      None,    'caller', 'exc'] # only if we have big issues
#DEBUG _stack_options = ['caller', 'exc',    'exc',   'exc',    'exc'] # exercise

class BaseLogger:
    """
    BaseLog: for use by all basestation code and utilities
    """
    self = None # the global instance
    is_initialized = False
    opts = None # whatever starting options
    log = None # the logger
    # for transient string logging
    stringHandler = None
    stringBuffer = None
    # Turns out that calling the logging calls is expensive
    # it always generates its own line info for a logging record before
    # finally handing it off to each handler
    # But we know what we want so record locallyand use to short-circuit these calls

    # warnings, errors, and criticals are always enabled
    # -v turns on log_info, ---debug turns on log_debug
    debug_enabled = info_enabled = False
    debug_loc, info_loc, warning_loc, error_loc, critical_loc = _stack_options

    # support adding messages to a set of final alerts to send to pilots
    # these are keyed by a section and  are an appended list of strings
    alerts_d = {}
    conversion_alerts_d = {}

    def __init__(self, name, opts=None, include_time=False):
        """
        Initializes a logging.Logger object, according to options (opts).
        """

        if (BaseLogger.is_initialized == False):
            BaseLogger.self = self
            BaseLogger.opts = opts

            #create logger
            BaseLogger.log = logging.getLogger(name)
            BaseLogger.log.setLevel(logging.DEBUG)

            # create a file handler if log filename is specified in opts
            if opts is not None:
                if (opts.base_log is not None and opts.base_log is not ""):
                    fh = logging.FileHandler(opts.base_log)
                    # fh.setLevel(logging.NOTSET) # messages of all levels will be recorded
                    self.setHandler(fh, opts, include_time)

            # always create a console handler
            sh = logging.StreamHandler()
            self.setHandler(sh, opts, include_time)

            BaseLogger.is_initialized = True
            log_info("Process id = %d" % os.getpid()) # report our process id

    def setHandler(self, handle, opts, include_time):
        """
        Set a logging handle.
        """
        if include_time:
            formatter = logging.Formatter("%(asctime)s: %(levelname)s: %(message)s", "%H:%M:%S %d %b %Y %Z")
        else:
            # Remove timestamps for easier log comparison and reading
            formatter = logging.Formatter("%(levelname)s: %(message)s")

        if opts is not None:
            if (opts.debug):
                BaseLogger.debug_enabled = True
                BaseLogger.info_enabled = True
                handle.setLevel(logging.DEBUG)
            elif (opts.verbose):
                BaseLogger.info_enabled = True
                handle.setLevel(logging.INFO)
            else:
                handle.setLevel(logging.WARNING)

        else:
            handle.setLevel(logging.WARNING)

        handle.setFormatter(formatter)
        BaseLogger.log.addHandler(handle)

        logging.captureWarnings(True)
        warnings_logger = logging.getLogger("py.warnings")
        warnings_logger.addHandler(handle)

    def getLogger(self):
        """getLogger: access function to log (static member)
        """
        if not BaseLogger.is_initialized:
            # error condition
            pass

        return BaseLogger.log


    def startStringCapture(self,include_time=False):
        """
        Start capturing all logging traffic to a string
        """
        if (self.stringHandler):
            # already capturing...probably not closed properly from previous call due to bailing out or exception handling
            self.stopStringCapture() # close handler and drop string on the floor

        self.stringBuffer = StringIO() # start a string stream
        self.stringHandler = logging.StreamHandler(self.stringBuffer)
        self.setHandler(self.stringHandler, self.opts, include_time)

    def stopStringCapture(self):
        """
        Stop capturing logging traffic to a string and return results
        """
        if (self.stringHandler is None):
            return "" # not capturing, return an empty string
        else:
            self.log.removeHandler(self.stringHandler)
            self.stringHandler.flush()
            self.stringHandler = None

            self.stringBuffer.flush()
            value = self.stringBuffer.getvalue()
            self.stringBuffer = None
            return value

def __log_caller_info(s, loc):
    """Add stack or module: line number info for log caller to given string
    Input:
    s - string to be logged

    Return:
    string with possible location information added
    """
    if loc:
        try:
            # skip our local callers
            offset = 3; # __log_caller_info(); log_XXXX; <caller>
            if loc in ['caller', 'parent']:
                if loc == 'parent': # A utlity routine
                    offset = offset + 1
                frame = traceback.extract_stack(None, offset)[0]
                module, lineno, function, _ = frame
                module = os.path.basename(module) # lose extension
                s = "%s(%d): %s" % (module, lineno, s)
            elif loc == 'exc':
                exc = traceback.format_exc()
                if exc: # if no exception, nothing added
                    s = "%s:\n%s" % (s, exc)
            elif loc == 'stack':
                frame_num = 0
                prefix = '>'
                stack = ''
                frames = traceback.extract_stack()
                # normally from bottom to top; reverse this so most recent call first
                frames.reverse()
                frames = frames[offset-1:-1] # drop our callers
                for frame in frames:
                    module, lineno, function, _ = frame # avoid the source code text
                    module = os.path.basename(module) # lose extension
                    stack = "%s\n%s %s(%d) %s()" % (stack, prefix, module, lineno, function)
                    prefix = ' '
                s = "%s:%s" % (s, stack)
            else: # unknown location request
                s = "(%s?): %s" % (loc, s)
        except:
            pass
    return s

def log_alerts():
    return BaseLogger.alerts_d

def log_conversion_alerts():
    return BaseLogger.conversion_alerts_d

def log_conversion_alert(key, s):
    conversion_alerts_d = BaseLogger.conversion_alerts_d
    if key not in conversion_alerts_d:
        conversion_alerts_d[key] = []
    conversion_alerts_d[key].append(s)

def log_alert(key, s):
    alerts_d = BaseLogger.alerts_d
    if key not in alerts_d:
        alerts_d[key] = []
    alerts_d[key].append(s)

# alert=None argument is optional to the log_X functions
# for easy searching, call like:
# log_warning("You got issues boss",alert='Salinity processing')

def log_critical(s,loc=BaseLogger.critical_loc,alert=None):
    """Report string to baselog as a CRITICAL error
    Input:
    s - string
    """
    s =__log_caller_info(s, loc)
    if alert:
        log_alert(alert, "CRITICAL: %s" % s)
    if(BaseLogger.log):
        BaseLogger.log.critical(s)
    else:
        sys.stderr.write("CRITICAL: %s\n" % s)

log_error_max_count = collections.defaultdict(int)
def log_error(s,loc=BaseLogger.error_loc,alert=None,max_count=None):
    """Report string to baselog as an ERROR
    Input:
    s - string
    """
    s =__log_caller_info(s, loc)

    if max_count:
        k = s.split(':')[0]
        log_error_max_count[k] += 1
        if log_error_max_count[k] == max_count:
            s += " (Max message count exceeded)"
        elif log_error_max_count[k] > max_count:
            return

    if alert:
        log_alert(alert, "ERROR: %s" % s)

    if(BaseLogger.log):
        BaseLogger.log.error(s)
    else:
        sys.stderr.write("ERROR: %s\n" % s)

log_warning_max_count = collections.defaultdict(int)
def log_warning(s,loc=BaseLogger.warning_loc,alert=None, max_count=None):
    """Report string to baselog as a WARNING
    Input:
    s - string
    """
    s =__log_caller_info(s, loc)

    if max_count:
        k = s.split(':')[0]
        log_warning_max_count[k] += 1
        if log_warning_max_count[k] == max_count:
            s += " (Max message count exceeded)"
        elif log_warning_max_count[k] > max_count:
            return

    if alert:
        log_alert(alert, "WARNING: %s" % s)
    if(BaseLogger.log):
        BaseLogger.log.warning(s)
    else:
        sys.stderr.write("WARNING: %s\n" % s)

log_info_max_count = collections.defaultdict(int)
def log_info(s,loc=BaseLogger.info_loc,alert=None, max_count=None):
    """Report string to baselog as an ERROR
    Input:
    s - string
    """
    if not BaseLogger.info_enabled:
        return
    s =__log_caller_info(s, loc)

    if max_count:
        k = s.split(':')[0]
        log_info_max_count[k] += 1
        if log_info_max_count[k] == max_count:
            s += " (Max message count exceeded)"
        elif log_info_max_count[k] > max_count:
            return

    if alert:
        log_alert(alert, "INFO: %s" % s)
    if(BaseLogger.log):
        BaseLogger.log.info(s)
    else:
        sys.stderr.write("INFO: %s\n" % s)

log_debug_max_count = collections.defaultdict(int)        
def log_debug(s,loc=BaseLogger.debug_loc,alert=None, max_count=None):
    """Report string to baselog as DEBUG info
    Input:
    s - string
    """
    if not BaseLogger.debug_enabled:
        return
    s =__log_caller_info(s, loc)

    if max_count:
        k = s.split(':')[0]
        log_debug_max_count[k] += 1
        if log_debug_max_count[k] == max_count:
            s += " (Max message count exceeded)"
        elif log_debug_max_count[k] > max_count:
            return
    
    if alert:
        log_alert(alert, "DEBUG: %s" % s)
    if(BaseLogger.log):
        BaseLogger.log.debug(s)
    else:
        sys.stderr.write("DEBUG: %s\n" % s)
