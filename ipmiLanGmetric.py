#!/usr/bin/env python

# (c) Robin Humble 2009-2013
# licensed under the GPL v3

# get temps (and power, fans, ...) from bmc/ilom/... and wedge them into ganglia
#  - slurp up a ganglia XML file to find which hosts are up
#  - run and parse ipmi-sensors output
#  - pound that info into ganglia via gmetric spoofing

import sys
import time
import string
import subprocess
import socket
import signal
import os

import add
from pbsMauiGanglia import gangliaStats

# only get temperatures from up nodes that we've heard from in the last 'aliveTime' seconds
aliveTime = 120

# sleep this many seconds between samples
sleepTime = 60

# kill the ipmi sub-process if it is taking too long
killTime = 600

# unreliable hosts
unreliable = []
# all cmm's aren't ipmi-sensors aware, so filter them out
for f in range(1,65):
   unreliable.append( 'cmm%d' % f )
for f in range(1,27):
   unreliable.append( 'hamster%d' % f + 'gige' )
for s in [ 'vu-man', 'gopher', 'vayu' ]:
   for f in range(1,5):
      unreliable.append( s + '%d' % f + 'gige' )
#unreliable.append( 'mayo' )
#unreliable.append( 'lolza' )
unreliable.append( 'roffle' )
unreliable.append( 'rofflegige' )
unreliable.append( 'knet00' )
unreliable.append( 'knet01' )

# xe210 nodes give -ve temps. take this number from coretemp. still a bit of a guess though...
intelTempOffset=80

# pick one
machine='fj'
machine='sun'

if machine == 'fj':
   # add this suffix to get the ilom/bmc name for the host
   manSuffix='bmc'

   # username/passwd for ipmi
   user=something
   passwd=something

   # different hosts to ignore...
   unreliable.append( 'vu-pbs' )
   unreliable.append( 'vu-test' )
   for f in range(1,1493):
      unreliable.append( 'v%d' % f )
   for f in range(1,27):
      unreliable.append( 'hamster%d' % f )
   for s in [ 'vu-man', 'gopher', 'vayu', 'marmot', 'spare' ]:
      for f in range(1,5):
         unreliable.append( s + '%d' % f )

elif machine == 'sun':
   # add this suffix to get the ilom/bmc name for the host
   manSuffix='ilom'

   # username/passwd for ipmi
   user=something
   passwd=something

else:
   print 'unknown machine'
   sys.exit(1)

ipCache = {}

singles = 'singleMachineGroup'

# compress a list of integers into the minimal cexec-like list
def compressList(l):
    l = sorted(l)
    c = []
    start = -1
    end = -1
    for i in range(len(l)):
        if start == -1:
            start = l[i]
        if i == len(l)-1 or l[i]+1 != l[i+1]:
            c.append( (start, end) )
            start = -1
            end = -1
        else:
            end = l[i+1]

    s = ''
    last = len(c)-1
    for i, (start, end) in enumerate(c):
        if end == -1:
            s += '%d' % start
        else:
            s += '%d-%d' % ( start, end )
        if i != last:
            s += ','

    return s

def findUpDown(all, timeout):
    now = time.time()  # seconds since 1970
    up = []
    down = []
    for host in all.keys():
        if now - all[host]['reported'] < timeout:
             up.append(host)
        else:
             down.append(host)
    return up, down

def listOfUpHosts(deadTimeout):
    g = gangliaStats( reportTimeOnly=1 )
    all = g.getAll()

    up, down = findUpDown(all, deadTimeout)
    up.sort()

    # delete hosts with unreliable bmc's
    for u in unreliable:
        if u in up:
            up.remove(u)

    # sort up hosts into groups with the same prefix
    uplist = {}
    uplist[singles] = []
    for u in up:
        # split into prefix (eg. 'x') and suffix (eg. 99)
        p = u.rstrip( string.digits )
        s = u[len(p):]
        #print u, s
        if len(s):
            if s not in unreliable:
                if p not in uplist.keys():
                    uplist[p] = []
                uplist[p].append(int(s))
        else:
            if p not in unreliable:
                uplist[singles].append(p)
    #print 'uplist', uplist

    # find the minimal lists of ipmi sequences
    ipmi = []
    for p, l in uplist.items():
        if p != singles:
            c = compressList(l)
            if ',' in c or '-' in c:
                c = '[' + c + ']'
            ipmi.append( p + c + manSuffix )
        else:
            for i in l:
                ipmi.append( i + manSuffix )

    # concatenate
    ipmiHosts = ''
    for i in ipmi:
        ipmiHosts += i + ','
    ipmiHosts = ipmiHosts[:-1]
    #print 'ipmiHosts', ipmiHosts

    return ipmiHosts, up

def alarmHandler(signum, frame):
   raise IOError("ipmi-sensors hung")

def alarmSet(t):
   signal.signal(signal.SIGALRM, alarmHandler)
   signal.alarm(t)

def alarmClear():
   signal.alarm(0)   

def runIpmiCommand( ipmiHosts, cmd ):
   # run ipmi-sensors
   s = 'ipmi-sensors --session-timeout=40000 -h \'' + ipmiHosts + '\' -u ' + user + ' -p \'' + passwd + '\' ' + cmd
   p = subprocess.Popen( s, shell=True, bufsize=-1, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
      
   # set a watchdog alarm as ipmi sometimes hangs
   alarmSet(killTime)

   try:
      out, err = p.communicate()
   except IOError:
      os.kill(p.pid, 15)
      p.wait()

   alarmClear()

   if p.returncode < 0:
      sys.stderr.write( 'ipmiLanGmetric: Error: ipmi-sensors: failed or was killed. return code %d\n' % p.returncode )
      return None 

   r = out.split('\n')
   # write any errs to stderr
   if len(err):
      # can get responses that look like ->
      #   cmm25: ipmi_cmd_get_sensor_reading: bad completion code: request data/parameter invalid
      #   cmm63: ipmi_cmd_get_sensor_reading: bad completion code: request data/parameter invalid
      # filter these out as they are usually missing/misbehaving blade power's
      # that we don't want or use anyway.
      # besides, any missing data that we really want will show up as errors
      # in postProcess checks.
      for e in err.split('\n'):
         if e == '' or 'ipmi_cmd_get_sensor_reading: bad completion code: request data/parameter invalid' in e:
            continue
         sys.stderr.write( 'ipmiLanGmetric: Error: ipmi-sensors: ' + str(e) + '\n' )
   return r

def getIp(host):
   try:
      ip = ipCache[host]
   except:
      #print 'host', host, 'not in ipCache'
      try:
         ip = socket.gethostbyname(host)
         ipCache[host] = ip
      except:
         ip = None
   return ip

def parseValsToGmetricLines( r ):
   # parse temps and generate gmetric commands, and a dict of responses
   c = []
   d = {}
   post = {}
   for l in r:
      if not len(l):  # last line can be '', so skip it
         continue
      l = l.split()

            # xe210
            # xemdsbmc: 11: Serverboard Tem (Temperature): 25.00 C (5.00/66.00): [OK]
            # xemdsbmc: 12: Ctrl Panel Temp (Temperature): 19.00 C (0.00/48.00): [OK]
            # xemdsbmc: 23: P1 Therm Margin (Temperature): -54.00 C (NA/NA): [OK]
            # xemdsbmc: 24: P2 Therm Margin (Temperature): -61.00 C (NA/NA): [OK]
            # xemdsbmc: 25: P1 Therm Ctrl % (Temperature): 0.00 unspecified (NA/49.53): [OK]
            # xemdsbmc: 26: P2 Therm Ctrl % (Temperature): 0.00 unspecified (NA/49.53): [OK]
            # xemdsbmc: 59: CPU1 VRD Temp (Temperature): [OK]
            # xemdsbmc: 60: CPU2 VRD Temp (Temperature): [OK]
            # xemdsbmc: 111: HSBP Temp (Temperature): NA(NA/NA): [Unknown]

            # supermicro
            # x22bmc: 4: CPU Temp 1 (Temperature): 31.00 C (NA/78.00): [OK]
            # x22bmc: 5: CPU Temp 2 (Temperature): 35.00 C (NA/78.00): [OK]
            # x22bmc: 6: CPU Temp 3 (Temperature): 0.00 C (NA/78.00): [OK]
            # x22bmc: 7: CPU Temp 4 (Temperature): 0.00 C (NA/78.00): [OK]
            # x23bmc: 8: Sys Temp (Temperature): 27.00 C (NA/78.00): [OK]

            # sun c48 ilom
            # v185ilom: 20: MB/T_AMB_FRONT (Temperature): 27.00 C (-5.00/55.00): [OK]
            # v185ilom: 21: MB/T_AMB_REAR (Temperature): 40.00 C (-5.00/55.00): [OK]

            # sun x4170/x4270/x4275 ilom
            # vu-man1ilom: 23: /MB/T_AMB (Temperature): 35.00 C (-5.00/55.00): [OK]
            # vu-man1ilom: 66: T_AMB (Temperature): 30.00 C (NA/45.00): [OK]

            #  # ipmi-sensors -D lan -h 'cmm[1,2]' -u ... -p ... -W endianseq -g Fan -g Power_Unit | grep cmm1
            # cmm1: 10752: fm0.f0.speed (Fan): 4100.00 RPM (NA/NA): [OK]
            # cmm1: 11008: fm0.f1.speed (Fan): 4200.00 RPM (NA/NA): [OK]
            # ...
            # cmm1: 49664: PS0/IN_POWER (Power Unit): 3750.00 W (NA/NA): [OK]
            # cmm1: 49920: PS0/OUT_POWER (Power Unit): 3360.00 W (NA/NA): [OK]
            # cmm1: 50176: PS1/IN_POWER (Power Unit): 3700.00 W (NA/NA): [OK]
            # cmm1: 50432: PS1/OUT_POWER (Power Unit): 3296.00 W (NA/NA): [OK]
            #
            # where
            #    PS0/IN_POWER and ps0.ac_watts  are about the same
            #   PS1/OUT_POWER and ps1.dc_watts    ""      ""      , but PS0/OUT_POWER != ps0.dc_watts :-/
            #         /CH/VPS and ch.ac_watts     ""      ""
            #   ps0.ac_watts+ps1.ac_watts (7500) ~= ch.ac_watts (7474)
            # as the ILOM advertises the upper case versions, I'll use them ie.
            #   PS0/1 IN_POWER and OUT_POWER
            #
            # 18176: ps0.t_amb (Temperature): 29.00 C (NA/NA): [OK]
            # 23040: ps0.t_amb_fault (Temperature): [Predictive Failure deasserted]
            # 24832: ps0.t_amb_warn (Temperature): [Predictive Failure deasserted]
            # 25856: ps1.t_amb (Temperature): 27.00 C (NA/NA): [OK]

            # newer ipmi-sensors 1.16 ->

            # cmm51: 2   | CMM/T_AMB     | Temperature | 45.00      | C     | 'OK'
            # cmm51: 3   | T_AMB         | Temperature | 29.00      | C     | 'OK'
            # cmm51: 68  | FM0/F0/TACH   | Fan         | 3700.00    | RPM   | 'OK'
            # cmm51: 69  | FM0/F1/TACH   | Fan         | 3800.00    | RPM   | 'OK'
            # cmm51: 85  | PS0/IN_POWER  | Power Unit  | 3550.00    | W     | 'OK'
            # cmm51: 86  | PS0/OUT_POWER | Power Unit  | 3150.00    | W     | 'OK'
            # cmm51: 88  | PS1/IN_POWER  | Power Unit  | 3500.00    | W     | 'OK'
            # cmm51: 89  | PS1/OUT_POWER | Power Unit  | 3100.00    | W     | 'OK'
            # cmm51: 90  | PS0/T_AMB     | Temperature | 29.00      | C     | 'OK'
            # cmm51: 98  | PS0/FAN0/TACH | Fan         | 12300.00   | RPM   | 'OK'
            # cmm51: 99  | PS0/FAN1/TACH | Fan         | 12420.00   | RPM   | 'OK'
            # cmm51: 100 | PS0/FAN2/TACH | Fan         | 12240.00   | RPM   | 'OK'
            # cmm51: 101 | PS0/FAN3/TACH | Fan         | 12120.00   | RPM   | 'OK'
            # cmm51: 120 | PS1/T_AMB     | Temperature | 30.00      | C     | 'OK'
            # cmm51: 128 | PS1/FAN0/TACH | Fan         | 12540.00   | RPM   | 'OK'
            # cmm51: 129 | PS1/FAN1/TACH | Fan         | 12600.00   | RPM   | 'OK'
            # cmm51: 150 | VPS           | Power Unit  | 7100.00    | W     | 'OK'
            # cmm51: 156 | BL5/VPS       | Power Unit  | 515.00     | W     | 'OK'
            # cmm51: 157 | BL6/VPS       | Power Unit  | 555.00     | W     | 'OK'

            # v51ilom: 22 | MB/T_AMB_FRONT | Temperature | 32.00      | C     | 'OK'
            # v51ilom: 23 | MB/T_AMB_REAR  | Temperature | 58.00      | C     | 'OK'
            # v51ilom: 24 | VPS            | Power Unit  | 281.40     | W     | 'OK'

            # hamster5ilom: 24 | /MB/T_AMB     | Temperature | 30.00      | C     | 'OK'
            # hamster5ilom: 41 | /SYS/VPS      | Power Unit  | 260.00     | W     | 'OK'
            # hamster5ilom: 52 | PS0/IN_POWER  | Power Unit  | 110.00     | W     | 'OK'
            # hamster5ilom: 53 | PS0/OUT_POWER | Power Unit  | 80.00      | W     | 'OK'
            # hamster5ilom: 64 | PS1/IN_POWER  | Power Unit  | 110.00     | W     | 'OK'
            # hamster5ilom: 65 | PS1/OUT_POWER | Power Unit  | 80.00      | W     | 'OK'
            # hamster5ilom: 66 | T_AMB         | Temperature | 24.00      | C     | 'OK'

            # sw 2.6.1
            # gopher4ilom: 24 | /MB/T_AMB     | Temperature | N/A        | C     | N/A
            # gopher4ilom: 41 | /SYS/VPS      | Power Unit  | 210.00     | W     | 'OK'
            # gopher4ilom: 52 | PS0/IN_POWER  | Power Unit  | 90.00      | W     | 'OK'
            # gopher4ilom: 53 | PS0/OUT_POWER | Power Unit  | 70.00      | W     | 'OK'
            # gopher4ilom: 64 | PS1/IN_POWER  | Power Unit  | 120.00     | W     | 'OK'
            # gopher4ilom: 65 | PS1/OUT_POWER | Power Unit  | 90.00      | W     | 'OK'
            # gopher4ilom: 66 | T_AMB         | Temperature | 25.00      | C     | 'OK'

            # cx250
            # bm4bmc: 1  | CPU0_Temp        | Temperature | 84.00      | C     | 'OK'
            # bm4bmc: 2  | CPU1_Temp        | Temperature | 75.00      | C     | 'OK'
            # bm4bmc: 17 | HDDBP_Ambient1   | Temperature | 34.00      | C     | 'OK'
            # bm4bmc: 18 | HDDBP_Ambient2   | Temperature | 28.00      | C     | 'OK'
            # bm4bmc: 19 | PDB_FAN1A        | Fan         | N/A        | RPM   | N/A
            # bm4bmc: 20 | PDB_FAN2A        | Fan         | N/A        | RPM   | N/A
            # bm4bmc: 21 | PDB_FAN3A        | Fan         | 9300.00    | RPM   | 'OK'
            # bm4bmc: 22 | PDB_FAN4A        | Fan         | 9300.00    | RPM   | 'OK'
            # bm4bmc: 32 | P0_DIMM_Temp     | Temperature | 52.00      | C     | 'OK'
            # bm4bmc: 33 | P1_DIMM_Temp     | Temperature | 48.00      | C     | 'OK'
            # bm4bmc: 37 | MB2_Temp         | Temperature | 60.00      | C     | 'OK'
            # bm4bmc: 38 | MB1_Temp         | Temperature | 59.00      | C     | 'OK'
            # bm4bmc: 48 | PDB_FAN1B        | Fan         | N/A        | RPM   | N/A
            # bm4bmc: 49 | PDB_FAN2B        | Fan         | N/A        | RPM   | N/A
            # bm4bmc: 50 | PDB_FAN3B        | Fan         | 8200.00    | RPM   | 'OK'
            # bm4bmc: 51 | PDB_FAN4B        | Fan         | 8200.00    | RPM   | 'OK'
            # bm4bmc: 52 | PSU_Input_Power  | Current     | 192.00     | W     | 'OK'

            # rx300
            # sf1bmc: 32   | Ambient       | Temperature | 28.50      | C     | 'OK'
            # sf1bmc: 96   | Systemboard 1 | Temperature | 30.00      | C     | 'OK'
            # sf1bmc: 160  | Systemboard 2 | Temperature | 36.00      | C     | 'OK'
            # sf1bmc: 224  | CPU1          | Temperature | 40.00      | C     | 'OK'
            # sf1bmc: 288  | CPU2          | Temperature | 41.00      | C     | 'OK'
            # sf1bmc: 352  | MEM A         | Temperature | 31.00      | C     | 'OK'
            # ...
            # sf1bmc: 800  | MEM H         | Temperature | 32.00      | C     | 'OK'
            # sf1bmc: 864  | PSU1 Inlet    | Temperature | 31.00      | C     | 'OK'
            # sf1bmc: 928  | PSU2 Inlet    | Temperature | 30.00      | C     | 'OK'
            # sf1bmc: 992  | PSU1          | Temperature | 54.00      | C     | 'OK'
            # sf1bmc: 1056 | PSU2          | Temperature | 53.00      | C     | 'OK'
            # sf1bmc: 2016 | FAN1 SYS      | Fan         | 6300.00    | RPM   | 'OK'
            # sf1bmc: 2080 | FAN2 SYS      | Fan         | 6480.00    | RPM   | 'OK'
            # sf1bmc: 2144 | FAN3 SYS      | Fan         | 6120.00    | RPM   | 'OK'
            # sf1bmc: 2208 | FAN4 SYS      | Fan         | 6480.00    | RPM   | 'OK'
            # sf1bmc: 2272 | FAN5 SYS      | Fan         | 6480.00    | RPM   | 'OK'
            # sf1bmc: 2336 | FAN PSU1      | Fan         | 1280.00    | RPM   | 'OK'
            # sf1bmc: 2400 | FAN PSU2      | Fan         | 1360.00    | RPM   | 'OK'
            # sf1bmc: 2464 | CPU1 Power       | Other Units Based Sensor | 4.00       | W     | 'OK'
            # sf1bmc: 2528 | CPU2 Power       | Other Units Based Sensor | 6.00       | W     | 'OK'
            # sf1bmc: 2592 | System Power     | Other Units Based Sensor | 62.00      | W     | 'OK'
            # sf1bmc: 2656 | HDD Power        | Other Units Based Sensor | 6.00       | W     | 'OK'
            # sf1bmc: 2720 | PSU1 Power       | Other Units Based Sensor | 60.00      | W     | 'OK'
            # sf1bmc: 2784 | PSU2 Power       | Other Units Based Sensor | 60.00      | W     | 'OK'
            # sf1bmc: 2848 | Total Power      | Other Units Based Sensor | 120.00     | W     | 'OK'
            # sf1bmc: 2912 | Total Power Out  | Other Units Based Sensor | 96.00      | W     | 'OK'


      vOffset = 0
      cmm = len(l[0]) > 3 and l[0][:3] == 'cmm'

      # rx300
      if l[3] == 'Total' and  l[4] == 'Power' and  l[5] == '|':
         vField = 11
         dClass = 'power'
         dTag = 'node_power'
         dType = 'float'
         unit = 'W'
      elif l[3] in ( 'CPU1', 'CPU2' ) and l[5] == 'Temperature':
         vField = 7
         dClass = 'temp'
         dTag = 'cpu' + l[3][3] + '_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[3] == 'Systemboard':   # pick one at random
         vField = 8
         dClass = 'temp'
         dTag = 'chassis_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[3] == 'Ambient' and l[5] == 'Temperature' and l[7] != 'N/A':
         vField = 7
         dClass = 'temp'
         dTag = 'ambient_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[3][:3] == 'FAN' and l[4] == 'SYS':
         vField = 8
         dClass = 'fans'
         dTag = 'fan' + l[3][3]   # eg. fan1-fan5
         dTag = dTag.lower()
         dType = 'uint32'
         unit = 'RPM'

      # cx250
      elif l[3] in ( 'CPU0_Temp', 'CPU1_Temp' ):
         vField = 7
         dClass = 'temp'
         dTag = 'cpu' + str(int(l[3][3])+1) + '_temp'  # change 0,1 to 1,2
         dType = 'uint32'
         unit = 'C'
      elif l[3] in ( 'MB1_Temp', 'MB2_Temp' ):   # pick one at random
         vField = 7
         dClass = 'temp'
         dTag = 'chassis_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[3] in ( 'HDDBP_Ambient1', 'HDDBP_Ambient2' ):   # disk temps, pick one at random to use as ambient
         vField = 7
         dClass = 'temp'
         dTag = 'ambient_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[3][:7] == 'PDB_FAN' and l[7] != 'N/A':  # nodes see either 1a,1b,2a,2b or 3a,3b,4a,4b
         vField = 7
         dClass = 'fans'
         dTag = 'fan' + l[3][7:]   # eg. fan3a
         dTag = dTag.lower()
         dType = 'uint32'
         unit = 'RPM'
      elif l[3] == 'PSU_Input_Power':
         vField = 7
         dClass = 'power'
         dTag = 'node_power'
         dType = 'float'
         unit = 'W'

      elif l[3] in ( 'MB/T_AMB_FRONT', '/MB/T_AMB' ):  # ilom T
         vField = 7
         if l[3] == '/MB/T_AMB' and l[vField] == 'N/A': # busted. skip on new lynx 2.6.1
            continue
         dClass = 'temp'
         dTag = 'front_temp'
         dType = 'uint32'
         unit = 'C'
      #elif l[2] == 'MB/T_AMB_REAR':  # ilom T
      #   vField = 4
      elif l[3] == 'MB/T_AMB_REAR':  # ilom T
         vField = 7
         dClass = 'temp'
         dTag = 'rear_temp'
         dType = 'uint32'
         unit = 'C'
      # don't pickup cmm t_amb ~= ps0/1_temp by mistake
      #elif l[2] == 'T_AMB' and not cmm:
      #   vField = 4
      elif l[3] == 'T_AMB' and not cmm:  # ilom T_AMB for lynx's - close to ambient?
         vField = 7
         dClass = 'temp'
         dTag = 'ambient_temp'
         dType = 'uint32'
         unit = 'C'
      # don't pickup cmm power by mistake
      #elif l[2] in ( 'VPS', '/SYS/VPS' ) and not cmm:  # ilom node power
      #   vField = 5
      elif l[3] in ( 'VPS', '/SYS/VPS' ) and not cmm:  # ilom node power
         vField = 8
         dClass = 'power'
         dTag = 'node_power'
         dType = 'float'
         unit = 'W'
      # limit this to cmm's as lynx's also have PS0/*
      #elif cmm and l[2] in ( 'PS0/IN_POWER', 'PS1/IN_POWER', 'PS0/OUT_POWER', 'PS1/OUT_POWER' ):  # cmm power
      #   vField = 5
      #   dTag = 'cmm_ps' + l[2][2] + '_' + l[2].split('_')[0][4:].lower()   # eg. cmm_ps0_in, cmm_ps1_out ...
      elif cmm and l[3] in ( 'PS0/IN_POWER', 'PS1/IN_POWER', 'PS0/OUT_POWER', 'PS1/OUT_POWER' ):  # cmm power
         vField = 8
         dClass = 'power'
         dTag = 'cmm_ps' + l[3][2] + '_' + l[3].split('_')[0][4:].lower()   # eg. cmm_ps0_in, cmm_ps1_out ...
         dType = 'uint32'
         unit = 'W'
      elif cmm and len(l[2]) > 2 and l[2][:2] == 'fm' and len(l[2].split('.')) == 3 and l[2].split('.')[2] == 'speed':  # cmm v2 format fan speeds
         vField = 4
         dClass = 'fans'
         dTag = l[2][:3] + '_' + l[2][4:6]   # eg. fm0.f0.speed -> fm0_f0
         dTag = dTag.lower()  # just in case
         dType = 'uint32'
         unit = 'RPM'
      #elif cmm and len(l[2]) > 2 and l[2][:2] == 'FM' and len(l[2].split('/')) == 3 and l[2].split('/')[2] == 'TACH':  # cmm v3 format fan speeds
      #   vField = 4
      #   dTag = l[2][:3] + '_' + l[2][4:6]   # eg. 61: FM3/F0/TACH (Fan): 5600.00 RPM (NA/NA): [OK]  -> fm3_f0
      elif cmm and len(l[3]) > 2 and l[3][:2] == 'FM' and len(l[3].split('/')) == 3 and l[3].split('/')[2] == 'TACH':  # cmm v3 format fan speeds
         vField = 7
         dClass = 'fans'
         dTag = l[3][:3] + '_' + l[3][4:6]   # eg. 61: FM3/F0/TACH (Fan): 5600.00 RPM (NA/NA): [OK]  -> fm3_f0
         dTag = dTag.lower()  # just in case
         dType = 'uint32'
         unit = 'RPM'
      #elif cmm and l[2] in ( 'ps0.t_amb', 'ps1.t_amb', 'PS0/T_AMB', 'PS1/T_AMB' ):  # cmm ps0/1 temps - supposedly related to fan speeds
      #   vField = 4
      #   dTag = l[2][:3] + '_temp'  # eg. ps0.t_amb or PS0/T_AMB -> ps0_temp
      elif cmm and l[3] in ( 'ps0.t_amb', 'ps1.t_amb', 'PS0/T_AMB', 'PS1/T_AMB' ):  # cmm ps0/1 temps - supposedly related to fan speeds
         vField = 7
         dClass = 'temp'
         dTag = l[3][:3] + '_temp'  # eg. ps0.t_amb or PS0/T_AMB -> ps0_temp
         dTag = dTag.lower()
         dType = 'uint32'
         unit = 'C'
      #elif cmm and l[2] in ( 'ch.t_amb_0', 'T_AMB0', 'T_AMB' ):
      #   vField = 4
      elif cmm and l[3] in ( 'ch.t_amb_0', 'T_AMB0', 'T_AMB' ):  # cmm "chassis ambient" temps - only T_AMB0 from cmm v3, so ignore ch.t_amb_1 from cmm v2
                                                                 # later v3 has just T_AMB (as well as a CMM/T_AMB that we ignore)
         vField = 7
         dClass = 'temp'
         if l[3] == 'T_AMB':
            dTag = 'ch0_temp'
         else:
            dTag = 'ch' + l[3][-1] + '_temp'  # eg. ch.t_amb_0 -> ch0_temp
         dType = 'uint32'
         unit = 'C'

      elif l[4] == 'Margin':         # xe210 cpu
         vField = 6
         vOffset = intelTempOffset
         dClass = 'temp'
         dTag = 'cpu' + l[2][1] + '_temp'
         dType = 'int32'
         unit = 'C'
      elif l[2] == 'Serverboard':  # xe210 motherboard
         vField = 5
         dClass = 'temp'
         dTag = 'chassis_temp'
         dType = 'uint32'
         unit = 'C'
      elif l[4] in ( '1', '2' ):   # supermicro cpu
         vField = 6
         dClass = 'temp'
         dTag = 'cpu' + l[4] + '_temp'
         dType = 'int32'
         unit = 'C'
      elif l[2] == 'Sys':          # supermicro motherboard
         vField = 5
         dClass = 'temp'
         dTag = 'chassis_temp'
         dType = 'uint32'
         unit = 'C'
      else:
         continue

      host = l[0].rstrip( manSuffix + ':' )
      ip = getIp(host)
      if ip == None:
         continue
      spoofStr = ip + ':' + host

      try:
         if dType in ( 'int32', 'uint32' ):
            val = int(l[vField].split('.')[0]) + vOffset
            valStr = '%d' % val
         elif dType in ( 'float', 'double' ):
            val = float(l[vField]) + vOffset
            valStr = '%.2f' % val
         else:
            print 'unknown dType'
            sys.exit(1)
      except:
         sys.stderr.write( sys.argv[0] + ': Error converting value' + str(l) )
         continue

      #print host, dTag, val, valStr, spoofStr
      c.append( '/usr/bin/gmetric -S ' + spoofStr + ' -t ' + dType + ' -n "' + dTag + '" -u "' + unit + '" -v ' + valStr + '\n' )

      # keep a count of how many of which type of reponses
      if host not in d.keys():
         d[host] = {}
      if dClass not in d[host].keys():
         d[host][dClass] = 0
      d[host][dClass] += 1

      # store away all cmm stuff for post processing...
      if cmm:
         if host not in post.keys():
            post[host] = {}
         if dClass not in post[host].keys():
            post[host][dClass] = []
         post[host][dClass].append( ( dTag, val ) )

   return c, d, post

def checkResponseCounts(up, d):
   hosts = d.keys()
   for i in up:
      if i not in hosts:
         print 'host ' + i + ' did not respond'
         continue
      resp = d[i]
      respkeys = resp.keys()
      if i[0] == 'v' and i[1] in string.digits:
         # vayu blades have 2 temps and 1 power
         if 'power' not in respkeys or resp['power'] != 1:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' power incomplete\n' )
         if 'temp' not in respkeys or resp['temp'] != 2:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' temperatures incomplete\n' )
      elif len(i) > 3 and i[:3] == 'cmm':
         # cmm's have 16 fans and 4 power and 3 temps
         if 'power' not in respkeys or resp['power'] != 4:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' power incomplete\n' )
         if 'fans' not in respkeys or resp['fans'] != 16:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' fans incomplete\n' )
         if 'temp' not in respkeys or resp['temp'] != 3:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' temperatures incomplete\n' )
      elif i[:3] in ( 'sta' ):
         # rx300's have 5 fans, 5 temps, 1 power
         if 'power' not in respkeys or resp['power'] != 1:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' power incomplete\n' )
         if 'fans' not in respkeys or resp['fans'] != 5:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' fans incomplete\n' )
         if 'temp' not in respkeys or resp['temp'] != 5:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' temperatures incomplete\n' )
      elif i[:2] in ( 'rt' ):
         # cx250's have 4 fans, 6 temps, 1 power
         if 'power' not in respkeys or resp['power'] != 1:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' power incomplete\n' )
         if 'fans' not in respkeys or resp['fans'] != 4:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' fans incomplete\n' )
         if 'temp' not in respkeys or resp['temp'] != 6:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' temperatures incomplete\n' )
      else:
         # lynx's have 2 temps and 1 power
         if 'power' not in respkeys or resp['power'] != 1:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' power incomplete\n' )
         if 'temp' not in respkeys or resp['temp'] != 2:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' temperatures incomplete\n' )

def postProcess(up, post, c):
   postkeys = post.keys()
   if not len(postkeys):
      return

   # for each host in turn...
   for i in up:
      spoofStr = getIp(i) + ':' + i

      if i not in postkeys:
         sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' not in post\n' )
         continue
      h = post[i]

      # power ->
      #   add up the ps0 and ps1 and print a total for 'in' == cmm_power_in, 'out' == cmm_power_out
      if 'power' not in h.keys():
         sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' no power field\n' )
      else:
         p = h['power']
         if len(p) != 4:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' not 4 power entries\n' )
         p_in = 0.0
         p_out = 0.0
         inCnt = 0
         outCnt = 0
         for t, v in p:
            # tag, val -> cmm_ps0_in, cmm_ps1_out, ...
            if t[-3:] == '_in':
               p_in += float(v)
               inCnt += 1
            else:
               p_out += float(v)
               outCnt += 1

         if inCnt == 2:
            c.append( '/usr/bin/gmetric -S ' + spoofStr + ' -t float -n "cmm_power_in" -u "W" -v %.2f\n' % p_in )
         if outCnt == 2:
            c.append( '/usr/bin/gmetric -S ' + spoofStr + ' -t float -n "cmm_power_out" -u "W" -v %.2f\n' % p_out )

      # fans ->
      #   add up all the fans on this cmm and compute stats
      if 'fans' not in h.keys():
         sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' no fans field\n' )
      else:
         p = h['fans']
         if len(p) != 16:
            sys.stderr.write( 'ipmiLanGmetric: Error: host ' + i + ' not 16 power entries\n' )
         ff = []
         for t, v in p:
            # fm0_f0 ... fm7_f1
            ff.append( str(v) )
         a = add.add()
         a.ll = ff
         a.process()

         dtype = 'float'
         d = 'fan_'
         unit = 'RPM'
         for f in ( 'ave', 'rms', 'min', 'max', 'sigma' ):
            val = a.q[f][0]
            # maybe add -x maxTime ?
            c.append( '/usr/bin/gmetric -S ' + spoofStr + ' -t ' + dtype + ' -n "' + d + f + '" -u "' + unit + '" -v %.2f\n' % val )


if __name__ == '__main__':
   first = 1
   cmmPass = 0

   while 1:
      if not first:
         if machine == 'fj':
            time.sleep(sleepTime)
         elif machine == 'sun':
            time.sleep(sleepTime/2)
      first = 0

      if not cmmPass:
         i, up = listOfUpHosts(aliveTime)
         #print 'i', i, 'up', up
         if machine == 'fj':
            ipmiGather = '-g Temperature -g Current -g Other_Units_Based_Sensor -g Fan'
            cmmPass = 0  # go back to self
         elif machine == 'sun':
            ipmiGather = '-g Temperature -g Power_Unit'
            cmmPass = 2  # skip 1
      elif cmmPass == 1:
         sys.exit(1)  # never go here now...
         # for older v2 cmm fw
         i = 'cmm[1-8]'
         up = []
         for f in range(1,8+1):
            up.append( 'cmm%d' % f )
         ipmiGather = '-W endianseq -g Fan -g Power_Unit -g Temperature'
         cmmPass = 2
      elif cmmPass == 2:
         #i = 'cmm[9-63]'
         i = 'cmm[1-63]'
         up = []
         #for f in range(9,63+1):
         for f in range(1,63+1):
            up.append( 'cmm%d' % f )
         ipmiGather = '-g Fan -g Power_Unit -g Temperature'
         cmmPass = 0
      #print i, up
      #sys.exit(1)
      #i = 'vayu1ilom,marmot[1-4]ilom,gopher1ilom,vu-man2ilom,hamster[1-4]ilom,vu-pbsilom'
      if not len(i):
         continue

      # gather vals from all up hosts
      r = runIpmiCommand(i, ipmiGather)
      if r == None:
         continue
      c, d, post = parseValsToGmetricLines(r)
      if not len(c): # no hosts up?
         continue
      #print 'c', c
      #print 'd', d
      #print 'post', post

      checkResponseCounts(up, d)

      # accumulate some of the cmm stats as new stats
      postProcess(up, post, c)
      #print 'c', c
      #continue

      # pump vals into ganglia via gmetric
      p = subprocess.Popen( '/bin/sh', shell=False, bufsize=-1, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
      for i in c:
         p.stdin.write( i )
      out, err = p.communicate()

      # ignore gmetric's spoof info line, send the rest to stderr
      for o in out.split('\n'):
         i = o.split()
         if len(i) and i[0].strip() != 'spoofName:':
            sys.stderr.write( 'ipmiLanGmetric: Error: gmetric stdout:' + str(i) + '\n' )

      # print err if any
      if len(err):
         sys.stderr.write( 'ipmiLanGmetric: Error: gmetric stderr: ' +  str(err) + '\n' )
