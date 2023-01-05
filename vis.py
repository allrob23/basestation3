#!/usr/bin/env python3.9

from orjson import dumps
import time
import os
import os.path
from parse import parse
import sys
from zipfile import ZipFile
from io import BytesIO
from anyio import Path
import aiosqlite
import aiofiles
import asyncio
import aiohttp
import sanic
import sanic_gzip
import sanic_ext
from functools import wraps,partial
import jwt
from passlib.hash import sha256_crypt
import uuid
from types import SimpleNamespace
import yaml
import LogHTML
import summary
import ExtractBinnedProfiles
import ExtractTimeseries
import multiprocessing

watchFiles = ['comm.log', 'cmdfile'] # rely on .urls vs '.completed']


PERM_REJECT = 0
PERM_VIEW   = 1
PERM_PILOT  = 2


compress = sanic_gzip.Compress()

publicMissionFields = {"started", "ended", "planned",
                       "orgname", "orglink", "contact", "email",
                       "project", "link", "comment", "reason"} # making this a dict does a set intersection

pilotModes = ['private', 'pilot']

# checks whether the auth token authorizes a user or group in users, groups
def checkToken(request, users, groups, pilots, pilotgroups):
    if not 'token' in request.cookies:
        return False

    perm = PERM_REJECT
    try:
         token = jwt.decode(request.cookies.get("token"), request.app.config.SECRET, algorithms=["HS256"])
    except jwt.exceptions.InvalidTokenError:
        return perm
    else:
        if users and 'user' in token and token['user'] in users:
            sanic.log.logger.debug(f"{token['user']} authorized [{request.path}]")
            perm = PERM_VIEW
        elif groups and 'groups' in token:
            # search the list of groups that this user is auth'd for
            # or check if this user has root
            for g in token['groups']:
                if g == 'root' or g in groups:
                    sanic.log.logger.debug(f"{token['user']} authorized based on group {g} [{request.path}]")
                    perm = PERM_VIEW
                    break

        if pilots and 'user' in token and token['user'] in pilots:
            perm = PERM_PILOT
            sanic.log.logger.debug(f"{token['user']} authorized to pilot [{request.path}]")
        elif pilotgroups and 'groups' in token:
            # search the list of groups that this user is auth'd for
            # or check if this user has root
            for g in token['groups']:
                if g in pilotgroups:
                    sanic.log.logger.debug(f"{token['user']} authorized to pilot based on group {g} [{request.path}]")
                    perm = PERM_PILOT
                    break

    return perm

# checks whether access is authorized for the glider,mission
def checkGliderMission(request, glider, mission, perm=PERM_VIEW):

    for m in request.app.ctx.missionTable:
        if m['glider'] == glider and m['mission'] == mission and \
            (m['users'] is not None or m['groups'] is not None \
             or m['pilotusers'] is not None or m['pilotgroups'] is not None): 
            return checkToken(request, m['users'], m['groups'], m['pilotusers'], m['pilotgroups'])
        elif m['glider'] == glider and m['mission'] == mission:
            return PERM_VIEW

    # no matching mission in table - do not allow access
    sanic.log.logger.debug(f'rejecting {glider} {mission} for no mission entry')
    return PERM_REJECT

def authorized(protections=None):
    def decorator(f):
        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            # run some method that checks the request
            # for the client's authorization status

            defaultPerm = PERM_VIEW

            # we never allow access to pilot APIs when running in public mode
            if protections and 'pilot' in protections and request.app.config.RUNMODE == 'public':
                sanic.log.logger.debug("rejecting no pilot APIs while running public")
                return sanic.response.text("Page not found: {}".format(request.path), status=404)
            # on an open pilot server (e.g., non-public server running 443) we require 
            # positive authentication as a pilot against mission specified list of 
            # allowed pilots (and pilotgroups). Access to missions without pilots: and/or pilotgroups: specs
            # will be denied for all. 
            elif protections and 'pilot' in protections and request.app.config.RUNMODE == 'pilot':
                requirePilot = True
            # if we're running a private instance of a pilot server then we only require authentication
            # as a pilot if the pilots/pilotgroups spec is given (similar to how users work)
            elif request.app.config.RUNMODE == 'private' and protections and 'pilot' in protections:
                requirePilot = True
                defaultPerm = PERM_PILOT 
            else:
                requirePilot = False

            glider = kwargs['glider'] if 'glider' in kwargs else None
            mission = request.args['mission'][0] if 'mission' in request.args else None
            
            # this will always fail and return not authorized if glider is None
            status = checkGliderMission(request, glider, mission, perm=defaultPerm)
            if status == PERM_REJECT or (requirePilot and status < PERM_PILOT):
                return sanic.response.text("authorization failed")
             
            # the user is authorized.
            # run the handler method and return the response
            response = await f(request, *args, **kwargs)
            return response
        return decorated_function
    return decorator

def purgeMessages(request):
    # assert isinstance(request.app.shared_ctx.urlMessages, list) # multiprocessing.managers.ListProxy)

    t = time.time() - 10
    for i in reversed(range(len(request.app.shared_ctx.urlMessages))):
        if request.app.shared_ctx.urlMessages[i]['time'] < t:
            del request.app.shared_ctx.urlMessages[i]

def rowToDict(cursor: aiosqlite.Cursor, row: aiosqlite.Row) -> dict:
    data = {}
    for idx, col in enumerate(cursor.description):
        data[col[0]] = row[idx]

    return data

def requestMission(request):
    if request and 'mission' in request.args and request.args['mission'] != 'current' and len(request.args['mission']) > 0:
        return request.args['mission'][0]

    return None

def gliderPath(glider, request, mission=None):
    if mission:
        return f'sg{glider:03d}/{mission}'
    else:
        mission = requestMission(request)
        if mission:
            return f'sg{glider:03d}/{mission}'
        else:
            return f'sg{glider:03d}'

def filterMission(gld, request, mission=None):
    if mission == None and \
       request and \
       'mission' in request.args and \
       request.args['mission'] != 'current' and \
       len(request.args['mission']) > 0:

        mission = request.args['mission'][0]
    
    m = next(filter(lambda d: d['glider'] == gld and d['mission'] == mission, request.app.ctx.missionTable), None)
    m = { k: m[k] for k in m.keys() & publicMissionFields } if m else None

    return m
#
# GET handlers - most of the API
#

def attachHandlers(app: sanic.Sanic):

    app.static('/favicon.ico', f'{sys.path[0]}/html/favicon.ico', name='favicon.ico')
    app.static('/parms', f'{sys.path[0]}/html/Parameter_Reference_Manual.html', name='parms')
    app.static('/script', f'{sys.path[0]}/scripts', name='script')
    app.static('/script/images', f'{sys.path[0]}/scripts/images', name='script_images')

    @app.exception(sanic.exceptions.NotFound)
    def pageNotFound(request, exception):
        return sanic.response.text("Page not found: {}".format(request.path), status=404)

    @app.post('/auth')
    async def authHandler(request):
        username = request.json.get("username", None)
        password = request.json.get("password", None)

        for user,prop in request.app.ctx.userTable.items():
            if user == username and sha256_crypt.verify(password, prop['password']):
                token = jwt.encode({ "user": username, "groups": prop['groups']}, request.app.config.SECRET)
                response = sanic.response.text("authorization ok")
                response.cookies["token"] = token
                response.cookies["token"]["max-age"] = 86400
                response.cookies["token"]["samesite"] = "Strict"
                response.cookies["token"]["httponly"] = True
                return response

        return sanic.response.text('authorization failed') 

    @app.route('/png/<which:str>/<glider:int>/<dive:int>/<image:str>')
    @authorized()
    async def pngHandler(request, which:str, glider: int, dive: int, image: str):
        if which == 'dv':
            filename = f'{gliderPath(glider,request)}/plots/dv{dive:04d}_{image}.png'
        elif which == 'eng':
            filename = f'{gliderPath(glider,request)}/plots/eng_{image}.png'
        elif which == 'section':
            filename = f'{gliderPath(glider,request)}/plots/sg_{image}.png'
        else:
            return sanic.response.text('not found', status=404)

        if await aiofiles.os.path.exists(filename):
            return await sanic.response.file(filename, mime_type='image/png')
        else:
            return sanic.response.text('not found', status=404)
            

    @app.route('/div/<which:str>/<glider:int>/<dive:int>/<image:str>')
    @authorized()
    async def divHandler(request, which: str, glider: int, dive: int, image: str):
        if which == 'dv':
            filename = f'{gliderPath(glider,request)}/plots/dv{dive:04d}_{image}.div'
        elif which == 'eng':
            filename = f'{gliderPath(glider,request)}/plots/eng_{image}.div'
        elif which == 'section':
            filename = f'{gliderPath(glider,request)}/plots/sg_{image}.div'
        else:
            return sanic.response.text('not found', status=404)

        if await aiofiles.os.path.exists(filename):
            mission = requestMission(request)
            mission = f"?mission={mission}" if mission else ''

            resp = '<script src="/script/plotly-latest.min.js"></script><html><head><title>%03d-%d-%s</title></head><body>' % (glider, dive, image)
            if which == 'dv':
                resp = resp + f'<a href="/div/{which}/{glider}/{dive-1}/{image}{mission}"style="text-decoration:none; font-size:32px;">&larr;</a><span style="font-size:32px;"> &#9863; </span> <a href="/div/{which}/{glider}/{dive+1}/{image}{mission}" style="text-decoration:none; font-size:32px;">&rarr;</a>'

            async with aiofiles.open(filename, 'r') as file:
                div = await file.read() 

            resp = resp + div + '</body></html>'
            return sanic.response.html(resp)
        else:
            return sanic.response.text('not found', status=404)
           
    # we don't protect this so they get a blank page with a login option even
    # if not authorized
    @app.route('/<glider:int>')
    @app.ext.template("vis.html")
    async def mainHandler(request, glider:int):
        # return await sanic_ext.render("vis.html", context={"runMode": request.app.config.RUNMODE}, status=400)
        runMode = request.app.config.RUNMODE
        if runMode == 'private':
            runMode = 'pilot'

        return {"runMode": runMode}

    @app.route('/dash')
    @app.ext.template("index.html")
    async def dashHandler(request):
        return {"runMode": "pilot"}

    @app.route('/')
    @app.ext.template("index.html")
    async def indexHandler(request):
        return {"runMode": "public"}

    @app.route('/map/<glider:int>')
    @authorized()
    async def mapHandler(request, glider:int):
        filename = f'{sys.path[0]}/html/map.html'
        return await sanic.response.file(filename, mime_type='text/html')

    @app.route('/map/<glider:int>/<extras:path>')
    @authorized()
    async def multimapHandler(request, glider:int, extras):
        filename = f'{sys.path[0]}/html/map.html'
        return await sanic.response.file(filename, mime_type='text/html')

    @app.route('/kml/<glider:int>')
    @authorized()
    async def kmlHandler(request, glider:int):
        filename = f'{gliderPath(glider,request)}/sg{glider}.kmz'
        async with aiofiles.open(filename, 'rb') as file:
            zip = ZipFile(BytesIO(await file.read()))
            kml = zip.open(f'sg{glider}.kml', 'r').read()
            return sanic.response.raw(kml)

    # do we need this for anything?? not yet
    @app.route('/data/<file:str>')
    @authorized(protections=['pilot'])
    async def dataHandler(request, file:str):
        filename = f'{sys.path[0]}/data/{file}'
        return await sanic.response.file(filename)

    @app.route('/proxy/<url:path>')
    # This is not a great idea to leave this open as a public proxy server,
    # but we need it for all layers to work with public maps at the moment.
    # Need to evaluate what we lose if we turn proxy off or find another solution.
    # Or limit the dictionary of what urls can be proxied ...
    # NOAA forecast, NIC ice edges, iop SA list, opentopo GEBCO bathy
    # @authorized(protections=['pilot'])
    async def proxyHandler(request, url):
        allowed = ['https://api.opentopodata.org/v1/gebco2020',
                   'https://marine.weather.gov/MapClick.php',
                   'https://iop.apl.washington.edu/', 
                   'https://usicecenter.gov/File/DownloadCurrent?pId',
                  ]

        found = False
        for x in allowed:
            if url.startswith(x):
                found = True
                break

        if found == False:
            return sanic.response.text(f"Page not found: {request.path}", status=404)
              
        if request.args and len(request.args) > 0:
            url = url + '?' + request.query_string
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    body = await response.read()
                    return sanic.response.raw(body)
        
    @app.route('/plots/<glider:int>/<dive:int>')
    @authorized()
    async def plotsHandler(request, glider:int, dive:int):
        (dvplots, plotlyplots) = await buildDivePlotList(gliderPath(glider,request), dive)
        message = {}
        message['glider']      = f'SG{glider:03d}'
        message['dive']        = dive
        message['dvplots']     = dvplots
        message['plotlyplots'] = plotlyplots
        # message['engplots']    = engplots
        
        return sanic.response.json(message)

    @app.route('/log/<glider:int>/<dive:int>')
    @authorized()
    async def logHandler(request, glider:int, dive:int):
        filename = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.log'
        s = await LogHTML.captureTables(filename)
        return sanic.response.html(s)

    @app.route('/file/<ext:str>/<glider:int>/<dive:int>')
    @authorized()
    async def logengcapFileHandler(request, ext:str, glider: int, dive: int):
        filename = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.{ext}'
        if await aiofiles.os.path.exists(filename):
            return await sanic.response.file(filename, mime_type='text/plain')
        else:
            if ext == 'cap':
                return sanic.response.text('none')
            else:
                return sanic.response.text('not found', status=404)
           
    @app.route('/alerts/<glider:int>/<dive:int>')
    @authorized()
    async def alertsHandler(request, glider: int, dive: int):
        filename = f'{gliderPath(glider,request)}/alert_message.html.{dive:d}'
        if await aiofiles.os.path.exists(filename):
            return await sanic.response.file(filename, mime_type='text/plain')
        else:
            return sanic.response.text('not found')
     
    @app.route('/deltas/<glider:int>/<dive:int>')
    @authorized()
    async def deltasHandler(request, glider: int, dive: int):
        cmdfile = f'{gliderPath(glider,request)}/cmdfile.{dive:d}'
        logfile = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.log'
        if not await aiofiles.os.path.exists(cmdfile) or not await aiofiles.os.path.exists(logfile):
            return sanic.response.text('not found')

        cmd = f"/usr/local/bin/validate {logfile} -c {cmdfile}"

        proc = await asyncio.create_subprocess_shell(
            cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        results = out.decode('utf-8', errors='ignore') 

        message = {}
        message['dive'] = dive
        message['parm'] = []    
        for line in results.splitlines():
            if "will change" in line:         
                pieces = line.split(' ')
                logvar = pieces[2]
                oldval = pieces[6]
                newval = pieces[8]
                message['parm'].append(f'{logvar},{oldval},{newval}')

        message['file'] = []

        files = ["science", "targets", "scicon.sch", "tcm2mat.cal", "pdoscmds.bat"]
        for f in files:
            filename = f'{gliderPath(glider,request)}/{f}.{dive}'

            if await aiofiles.os.path.exists(filename):
                async with aiofiles.open(filename, 'r') as file:
                    c = await file.read() 

                message['file'].append({ "file": f, "contents":c }) 

        return sanic.response.json(message)

    @app.route('/missions/<mask:str>')
    async def missionsHandler(request, mask:int):
        table = await buildAuthTable(request, mask)
        msg = { "missions": table, "organization": request.app.ctx.organization }
        return sanic.response.json(msg)
     
    @app.route('/summary/<glider:int>')
    @authorized()
    async def summaryHandler(request, glider:int):
        msg = await summary.collectSummary(glider, gliderPath(glider,request))
        msg['mission'] = filterMission(glider, request)
        return sanic.response.json(msg)

    # this does setup and is generally only called once at page load
    @app.route('/status/<glider:int>')
    @authorized()
    async def statusHandler(request, glider:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if await Path(dbfile).exists():
            async with aiosqlite.connect(dbfile) as conn:
                cur = await conn.cursor()
                try:
                    await cur.execute("SELECT dive FROM dives ORDER BY dive DESC LIMIT 1")
                except aiosqlite.OperationalError:
                    return sanic.response.text('no table')

                maxdv = (await cur.fetchone())[0]
        else:
            return sanic.response.text('file not found')

        (engplots, sgplots, engplotly, sgplotly) = await buildMissionPlotList(gliderPath(glider, request))

        message = {}
        message['glider'] = f'SG{glider:03d}'
        message['dive'] = maxdv
        message['engplots'] = engplots
        message['sgplots'] = sgplots
        message['engplotly'] = engplotly;
        message['sgplotly'] = sgplotly;
        message['organization'] = request.app.ctx.organization
        
        message['mission'] = filterMission(glider, request) 
        return sanic.response.json(message)

    @app.route('/control/<glider:int>/<which:str>')
    @authorized()
    async def controlHandler(request, glider:int, which:str):
        ok = ["cmdfile", "targets", "science", "scicon.sch", "tcm2mat.cal", "pdoscmds.bat", "sg_calib_constants.m"]

        if which not in ok:
            return sanic.response.text("oops")

        message = {}

        message['file'] = 'none'
        filename = f'{gliderPath(glider,request)}/{which}'

        if await aiofiles.os.path.exists(filename):
            message['file'] = which
            message['dive'] = -1
        else:
            p = Path(gliderPath(glider,request))
            latest = -1
            call = -1;
            async for fpath in p.glob(f'{which}.*'):
                try:
                    j = parse('%s.{:d}.{:d}' % which, fpath.name)
                    if j and hasattr(j, 'fixed') and len(j.fixed) == 2 and j.fixed[0] > latest and j.fixed[1] > call:
                        latest = j.fixed[0]
                        call = j.fixed[1]
                    else:
                        j = parse('%s.{:d}' % which, fpath.name)
                        if j and hasattr(j, 'fixed') and len(j.fixed) == 1 and j.fixed[0] > latest:
                            latest = j.fixed[0]
                            call = -1
                except Exception as e:
                    sanic.log.logger.info(f"controlHandler: {e}")
                    continue

            if latest > -1:
                message['file'] = which
                message['dive'] = latest
                if call > -1:
                    filename = f'{filename}.{latest}.{call}'
                    message['call'] = call
                else:
                    filename = f'{filename}.{latest}'
                    message['call'] = -1

        if message['file'] == "none":
            return sanic.response.text("none")

        async with aiofiles.open(filename, 'r') as file:
            message['contents']= await file.read() 

        return sanic.response.json(message)

    @app.route('/db/<glider:int>/<dive:int>')
    @authorized()
    async def dbHandler(request, glider:int, dive:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        q = "SELECT dive,log_start,log_D_TGT,log_D_GRID,log__CALLS,log__SM_DEPTHo,log__SM_ANGLEo,log_HUMID,log_TEMP,log_INTERNAL_PRESSURE,depth_avg_curr_east,depth_avg_curr_north,max_depth,pitch_dive,pitch_climb,batt_volts_10V,batt_volts_24V,batt_capacity_24V,batt_capacity_10V,total_flight_time_s,avg_latitude,avg_longitude,target_name,magnetic_variation,mag_heading_to_target,meters_to_target,GPS_north_displacement_m,GPS_east_displacement_m,flight_avg_speed_east,flight_avg_speed_north,dog_efficiency,alerts,criticals,capture,error_count FROM dives"

        if dive > -1:
            q = q + f" WHERE dive={dive};"
        else:
            q = q + " ORDER BY dive ASC;"

        async with aiosqlite.connect(dbfile) as conn:
            conn.row_factory = rowToDict # not async but called from async fetchall
            cur = await conn.cursor()
            try:
                await cur.execute(q)
            except aiosqlite.OperationalError:
                return sanic.response.text('no table')

            data = await cur.fetchall()
            # r = [dict((cur.description[i][0], value) \
            #       for i, value in enumerate(row)) for row in data]
            return sanic.response.json(data)

    @app.route('/dbvars/<glider:int>')
    @authorized()
    async def dbvarsHandler(request, glider:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        async with aiosqlite.connect(dbfile) as conn:
            cur = await conn.cursor()
            try:
                await cur.execute('select * from dives')
            except aiosqlite.OperationalError:
                return sanic.response.text('no table')
            names = list(map(lambda x: x[0], cur.description))
            data = {}
            data['names'] = names
            return sanic.response.json(data)

    @app.route('/provars/<glider:int>')
    @authorized()
    async def provarsHandler(request, glider:int):
        p = Path(gliderPath(glider,request))
        async for ncfile in p.glob(f'sg{glider:03d}*profile.nc'):
            data = {}
            data['names'] = ExtractBinnedProfiles.getVarNames(ncfile)
            return sanic.response.json(data)
         
        return sanic.response.text('oops')

    @app.route('/pro/<glider:int>/<which:str>/<first:int>/<last:int>/<stride:int>/<zStride:int>')
    @authorized()
    @compress.compress()
    async def proHandler(request, glider:int, which:str, first:int, last:int, stride:int, zStride:int):
        p = Path(gliderPath(glider,request))
        async for ncfile in p.glob(f'sg{glider:03d}*profile.nc'):
            data = ExtractBinnedProfiles.extractVar(ncfile, which, first, last, stride, zStride)
            return sanic.response.json(data)

        return sanic.response.text('oops')

    @app.route('/timevars/<glider:int>/<dive:int>')
    @authorized()
    async def timeSeriesVarsHandler(request, glider:int,dive:int):
        ncfile = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.nc'
        if await aiofiles.os.path.exists(ncfile):
            data = ExtractTimeseries.getVarNames(ncfile)
            return sanic.response.json(data)
        else: 
            return sanic.response.text('oops')

    @app.route('/time/<glider:int>/<dive:int>/<which:str>')
    @authorized()
    @compress.compress()
    async def timeSeriesHandler(request, glider:int, dive:int, which:str):
        ncfile = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.nc'
        if await aiofiles.os.path.exists(ncfile):
            data = ExtractTimeseries.extractVars(ncfile, which.split(','))
            return sanic.response.json(data)
        else:
            return sanic.response.text('oops')

    @app.route('/query/<glider:int>/<vars:str>')
    @authorized()
    async def queryHandler(request, glider, vars):
        pieces = vars.split(',')
        if pieces[0] == 'dive':
            q = f"SELECT {vars} FROM DIVES ORDER BY dive ASC"
        else:
            q = f"SELECT {vars} FROM DIVES"

        async with aiosqlite.connect(f'{gliderPath(glider,request)}/sg{glider:03d}.db') as conn:
            conn.row_factory = rowToDict
            cur = await conn.cursor()
            await cur.execute(q)
            data = await cur.fetchall()
            return sanic.response.json(data)

    @app.route('/selftest/<glider:int>')
    # @authorized(protections=['pilot'])
    async def selftestHandler(request, glider:int):
        cmd = f"{sys.path[0]}/SelftestHTML.py"
        proc = await asyncio.create_subprocess_exec(
            cmd, f"{glider:03d}", 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        results, err = await proc.communicate()
        return sanic.response.html(results.decode('utf-8', errors='ignore'))

    #
    # POST handler - to save files back to basestation
    #

    @app.post('/save/<glider:int>/<which:str>')
    @authorized(protections=['pilot'])
    async def saveHandler(request, glider:int, which:str):
        validator = {"cmdfile": "cmdedit", "science": "sciedit", "targets": "targedit"}

        message = request.json
        if 'file' not in message or message['file'] != which:
            return sanic.response.text('oops')

        path = gliderPath(glider, request)
        if which in validator:
            try:
                async with aiofiles.tempfile.NamedTemporaryFile('w') as file:
                    await file.write(message['contents'])
                    await file.close()
                    sanic.log.logger.debug("cosaved to %s" % file.name)

                    if 'force' in message and message['force'] == 1:
                        cmd = f"{sys.path[0]}/{validator[which]} -d {path} -q -i -f {file.name}"
                    else:
                        cmd = f"{sys.path[0]}/{validator[which]} -d {path} -q -f {file.name}"
                    
                    proc = await asyncio.create_subprocess_shell(
                        cmd, 
                        stdout=asyncio.subprocess.PIPE, 
                        stderr=asyncio.subprocess.PIPE
                    )
                    out, err = await proc.communicate()
                    results = out.decode('utf-8', errors='ignore') 
            except Exception as e:
                results = f"error saving {which}, {str(e)}"

            return sanic.response.text(results)

        else: # no validator for this file type
            try:
                async with aiofiles.open(f'{path}/{which}', 'w') as file:
                    await file.write(message['contents'])
                return sanic.response.text(f"{which} saved ok")
            except Exception as e:
                return sanic.response.text(f"error saving {which}, {str(e)}")
                
    #
    # web socket (real-time streams), including the get handler for notifications
    # from the basestation
    #

    @app.route('/url')
    async def urlHandler(request):
        # assert isinstance(request.app.shared_ctx.urlMessages, multiprocessing.managers.ListProxy)

        if 'instrument_name' not in request.args:
            return sanic.response.text('error')

        glider = int(request.args['instrument_name'][0][2:])
        dive   = int(request.args['dive'][0]) if 'dive' in request.args else None
        files  = request.args['files'][0] if 'files' in request.args else None
        status = request.args['status'][0] if 'status' in request.args else None
        gpsstr = request.args['gpsstr'][0] if 'gpsstr' in request.args else None

        if status:
            content = f"status={status}"
        elif gpsstr:
            content = f"gpsstr={gpsstr}"
        elif files:
            content = f"files={files}" 

        msg = { "glider": glider, "dive": dive, "content": content, "uuid": uuid.uuid4(), "time": time.time() }

        try:
            purgeMessages(request)
            request.app.shared_ctx.urlMessages.append(msg)
        except:
            pass
                 
        return sanic.response.text('ok')

    @app.websocket('/stream/<which:str>/<glider:int>')
    @authorized()
    async def streamHandler(request: sanic.Request, ws: sanic.Websocket, which:str, glider:int):
        # assert isinstance(request.app.shared_ctx.urlMessages, multiprocessing.managers.ListProxy)

        filename = f'{gliderPath(glider,request)}/comm.log'
        if not await aiofiles.os.path.exists(filename):
            await ws.send('no')
            return

        await ws.send(f"START") # send something to ack the connection opened

        sanic.log.logger.debug(f"streamHandler start {filename}")

        if request.app.config.RUNMODE in pilotModes:
            commFile = await aiofiles.open(filename, 'rb')
            if which == 'init':
                await commFile.seek(-10000, 2)
                data = await commFile.read()
                if data:
                    await ws.send(data.decode('utf-8', errors='ignore'))
            else:
                await commFile.seek(0, 2)
           
        watchList = await buildWatchList(request, glider) 
        prev_t = 0
        filename = f'{gliderPath(glider,request)}/cmdfile'
        while True:
            modFiles = await checkFileMods(watchList)
            if 'comm.log' in modFiles and request.app.config.RUNMODE in pilotModes:
                data = await commFile.read().decode('utf-8', errors='ignore')
                if data:
                    await ws.send(data)
            elif 'cmdfile' in modFiles and request.app.config.RUNMODE in pilotModes:
                async with aiofiles.open(filename, 'rb') as file:
                    body = (await file.read()).decode('utf-8', errors='ignore')
                    data = "CMDFILE=" + body
                    await ws.send(data)
            elif 'cmdfile' in modFiles:
                directive = await summary.getCmdfileDirective(filename)
                await ws.send(f"CMDFILE={directive}")
            else:
                purgeMessages(request)
                msg = list(filter(lambda m: m['glider'] == glider and m['time'] > prev_t, request.app.shared_ctx.urlMessages))
                prev_t = time.time()
                for m in msg:
                    await ws.send(f"NEW={glider},{m['dive']},{m['content']}")

            await asyncio.sleep(2)

    # not protected by decorator - buildAuthTable only returns authorized missions
    @app.websocket('/watch/<mask:str>')
    # @authorized(protections=['pilot'])
    async def watchHandler(request: sanic.Request, ws: sanic.Websocket, mask: str):
        # assert isinstance(request.app.shared_ctx.urlMessages, multiprocessing.managers.ListProxy)

        sanic.log.logger.debug("watchHandler start")
        opTable = await buildAuthTable(request, mask)
        prev_t = 0 
        await ws.send(f"START") # send something to ack the connection opened
        while True:
            purgeMessages(request)
            allMsgs = list(filter(lambda m: m['time'] > prev_t, request.app.shared_ctx.urlMessages))
            prev_t = time.time()

            for o in opTable:
                msg = list(filter(lambda m: m['glider'] == o['glider'], allMsgs))

                # We could block long enough here such that messages for 
                # other gliders might come in and we won't see them
                # because we're only checking outside the opTable loop.
                # Assume we'll get them next time around (2 seconds)
                for m in msg:
                    sanic.log.logger.debug(f"watch msg {m}")
                    await ws.send(f"NEW={o['glider']},{m['content']}")
                    
                if o['cmdfile'] is not None:
                    cmdfile = f"sg{o['glider']:03d}/cmdfile"
                    t = await aiofiles.os.path.getctime(cmdfile)
                    if t > o['cmdfile']:
                        directive = await summary.getCmdfileDirective(cmdfile)
                        sanic.log.logger.debug(f"watch {o['glider']} cmdfile modified")
                        await ws.send(f"NEW={o['glider']},cmdfile,{directive}")
                        o['cmdfile'] = t

            await asyncio.sleep(2) 

        sanic.log.logger.debug('watchHandler exit') # never gets here

    @app.listener("after_server_start")
    async def initApp(app, loop):
        await buildMissionTable(app)
        await buildUserTable(app)

        sanic.log.logger.info(f'STARTING runMode {app.config.RUNMODE}')

    @app.main_process_start
    async def mainProcessStart(app):
        app.shared_ctx.urlMessages =  multiprocessing.Manager().list([])

    @app.middleware('request')
    async def checkRequest(request):
        
        sanic.log.logger.info(f"checking {request.headers['host']} against {request.app.config.FQDN} in runMode {request.app.config.RUNMODE}")
        if request.app.config.RUNMODE != 'private' and request.app.config.FQDN not in request.headers['host']:
            sanic.log.logger.info(f"request for {request.headers['host']} blocked for lack of FQDN {request.app.config.FQDN}")
            return sanic.response.text('not found', status=404)

        return None


async def buildWatchList(request, glider):
    watchList = { "path": f'{gliderPath(glider,request)}' }

    for f in watchFiles: 
        filename = f'{gliderPath(glider,request)}/{f}' 
        if await aiofiles.os.path.exists(filename):
            t = await aiofiles.os.path.getctime(filename)
            watchList.update({ f:t })

    return watchList

async def checkFileMods(w):
    mod = []
    for f in watchFiles:
        filename = f"{w['path']}/{f}"
        if await aiofiles.os.path.exists(filename): # could assume exists if it's in the dict
            t = await aiofiles.os.path.getctime(filename)
            if t > w[f]:
                mod.append(f)
                w[f] = t

    return mod

#
#  other stuff (non-Sanic)
#

async def buildUserTable(app):

    if await aiofiles.os.path.exists(app.config.USERS_FILE):
        async with aiofiles.open(app.config.USERS_FILE, "r") as f:
            d = await f.read()
            try:
                x = yaml.safe_load(d)
            except Exception as e:
                sanic.log.logger.info(f"users parse error {e}")
                x = {}
    else:
        x = {}

    userDictKeys = [ "groups", "password" ]

    dflts = None
    for user in x.keys():
        if user == 'default':
            dflts = x[user]
            continue
        
        for uk in userDictKeys:
            if uk not in x[user].keys():
                x[user].update( { uk: dflts[uk] if uk in dflts else None } )

    app.ctx.userTable = x
    return x

async def buildMissionTable(app):

    if await aiofiles.os.path.exists(app.config.MISSIONS_FILE):
        async with aiofiles.open(app.config.MISSIONS_FILE, "r") as f:
            d = await f.read()
            x = yaml.safe_load(d)
    else:
        x = {}

    if 'organization' not in x:
        x['organization'] = {}
    if 'missions' not in x:
        x['missions'] = []

    missionDictKeys = [ "glider", "mission", "users", "pilotusers", "groups", "pilotgroups", 
                        "started", "ended", "planned", 
                        "orgname", "orglink", "contact", "email", 
                        "project", "link", "comment", "reason", 
                      ]
    dflts = None
    missions = []
    for k in x['missions'].keys():
        if k == 'defaults':
            dflts = x['missions'][k]
            continue
        
        pieces = k.split('/')
        if len(pieces) == 1:
            mission = None
        else:
            mission = pieces[1]

        try:
            glider = int(pieces[0][2:])
            x['missions'][k].update({ "glider":glider, "mission":mission })
            for mk in missionDictKeys:
                if mk not in x['missions'][k].keys():
                    if mk in dflts:
                        x['missions'][k].update( { mk: dflts[mk] })
                    else:
                        x['missions'][k].update( { mk: None })

            missions.append(x['missions'][k])
        except Exception as e:
            sanic.log.logger.info(f"error on key {k}, {e}")
            continue 
        
    orgDictKeys = ["name", "link", "text", "contact", "email"]
    for ok in orgDictKeys:
        if ok not in x['organization'].keys():
            x['organization'].update( { ok: None } )

    app.ctx.missionTable = missions
    app.ctx.organization = x['organization']

    return missions
 
async def buildAuthTable(request, mask):
    opTable = []
    for m in request.app.ctx.missionTable:
        status = checkGliderMission(request, m['glider'], m['mission'])
        if status == PERM_REJECT:
            continue

        cmdfile = f"{gliderPath(m['glider'], None, mission=m['mission'])}/cmdfile"
        if not await aiofiles.os.path.exists(cmdfile):
            continue

        if m['mission'] == None:
            t = await aiofiles.os.path.getctime(cmdfile)
            opTable.append({"mission": '', "glider": m['glider'], "cmdfile": t})
        else: 
            opTable.append({"mission": m['mission'], "glider": m['glider'], "cmdfile": None})

    return opTable

async def buildDivePlotList(path, dive):
    exts = [".png", ".div"] 
    plots = { ".png": [], ".div": [] }
    p = Path(path)
    p = p / 'plots' 
    
    async for fpath in p.glob(f"dv{dive:04d}_*.???"):
        if fpath.suffix in exts:
            x = parse('dv{}_{}.{}', fpath.name)
            plot = x[1] 
            plots[fpath.suffix].append(plot)
    
    return (plots[".png"], plots[".div"])
 
async def buildMissionPlotList(path):
    plots = { "eng": { ".png": [], ".div": [] }, "sg": { ".png": [], ".div": [] } }
    maxdv = -1
    p = Path(path)
    p = p / 'plots' 
    exts = ['.div', '.png']
    for prefix in ['eng', 'sg']:
        async for fpath in p.glob(f"{prefix}_*.???"):
            if prefix == 'sg' and '_section_' in fpath.name:
                continue

            if fpath.suffix in exts:
                plot = '_'.join(fpath.stem.split('_')[1:])
                plots[prefix][fpath.suffix].append(plot)

    return (plots['eng']['.png'], plots['sg']['.png'], plots['eng']['.png'], plots['sg']['.div'])


def createApp(runMode: str) -> sanic.Sanic:

    d = { "missionTable": [],   # list of missions (each a dict)
          "userTable": {},      # dict (keyed by username) of dict
          "runMode": runMode,
          "organization": {},
        }

    app = sanic.Sanic("SGpilot", ctx=SimpleNamespace(**d), dumps=dumps)
 
    if 'SECRET' not in app.config:
        app.config.SECRET = "SECRET"
    if 'MISSIONS_FILE' not in app.config:
        app.config.MISSIONS_FILE = "/home/seaglider/missions.dat"
    if 'USERS_FILE' not in app.config:
        app.config.USERS_FILE = "/home/seaglider/users.dat"
    if 'FQDN' not in app.config:
        app.config.FQDN = "seaglider.pub"

    app.config.update({"RUNMODE": runMode})
    app.config.TEMPLATING_PATH_TO_TEMPLATES=f"{sys.path[0]}/html"

    attachHandlers(app)

    return app

if __name__ == '__main__':
    root = os.getenv('SANIC_ROOTDIR')
    os.chdir(root if root is not None else '/home/seaglider')
    print(os.getcwd())

    runMode = 'private'

    if len(sys.argv) == 2:
        if sys.argv[1] == "public":
            port = 443
            runMode = 'public'
        else:
            port = int(sys.argv[1])
            if port == 4430:
                runMode = 'pilot'
    else:
        port = 20001


    loader = sanic.worker.loader.AppLoader(factory=partial(createApp, runMode))
    app = loader.load()
    if port == 443:
        ssl = {
            "cert": "/etc/letsencrypt/live/www.seaglider.pub/fullchain.pem",
            "key": "/etc/letsencrypt/live/www.seaglider.pub/privkey.pem",
            # "password": "for encrypted privkey file",   # Optional
        }
        app.prepare(host="0.0.0.0", port=443, ssl=ssl, access_log=True, debug=False, fast=True)

        sanic.Sanic.serve(primary=app, app_loader=loader)
        # app.config.update({"PORT": 443})
        #app.run(host="0.0.0.0", port=443, ssl=ssl, access_log=True, debug=False)
    else:
        app.prepare(host="0.0.0.0", port=port, access_log=True, debug=False)
        sanic.Sanic.serve(primary=app, app_loader=loader)

        # app.run(host='0.0.0.0', port=port, access_log=True, debug=True, fast=True)
