##
## Copyright (c)  2020, 2023 University of Washington.  All rights reserved.
##
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
##

# To support rebuilding a deployment from scratch, ensure raw data files
# are lower-cased (see globs in FileMgr), and that required files are present.
import sys
import os
import glob
import shutil

if __name__ == "__main__":
    # to rebuild the world
    for required_file in ["comm.log", "sg_calib_constants.m"]:
        if not os.path.exists(required_file):
            print(f"Missing required file: {required_file}")
            sys.exit(0)

    report_files_moved = False
    num_files_moved = 0
    # glob is case-sensitive so we find the upper-cased versions of transmitted files
    # rename archive (A) files that can from a flash card dump rather than transmitted
    for g in [
        "[A-Z][A-Z][0-9][0-9][0-9][0-9][LDKP][UZTG].[XA]",
        "[A-Z][A-Z][0-9][0-9][0-9][0-9][AB][UZTG].[XA]",
        "[a-z][a-z][0-9][0-9][0-9][0-9][ldkp][uztg].[xa]",
        "[a-z][a-z][0-9][0-9][0-9][0-9][ab][uztg].[xa]",
    ]:
        for fn in glob.glob(g):
            fnx = fn.lower()
            fnx = fnx[:-1] + "x"  # esnure x extension
            if report_files_moved:
                print(f"{fn} -> {fnx}")
            num_files_moved += 1
            shutil.move(fn, fnx)
    # TODO what about removing fragment files *.X00 etc?
    # Note that the fragment numbers are in hex (with K for C)
    if num_files_moved:
        print("Renamed %d data files" % num_files_moved)

    sys.exit(1)
