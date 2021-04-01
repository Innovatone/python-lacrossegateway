# Copyright (c) 2017 Heiko Thiery
# Copyright (c) 2021 Oliver Novakovic
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA

from __future__ import unicode_literals
import logging
import re
import threading
import socket

_LOGGER = logging.getLogger(__name__)

"""
    Jeelink lacrossegateway firmware commands
    <n>a     set to 0 if the blue LED bothers
    <n>f     initial frequency in kHz (5 kHz steps, 860480 ... 879515)  (for RFM
    #1)
    <n>F     initial frequency in kHz (5 kHz steps, 860480 ... 879515)  (for RFM
    #2)
    <n>h     altituide above sea level
    <n>m     bits 1: 17.241 kbps, 2 : 9.579 kbps, 4 : 8.842 kbps (for RFM #1)
    <n>M     bits 1: 17.241 kbps, 2 : 9.579 kbps, 4 : 8.842 kbps (for RFM #2)
    <n>r     use one of the possible data rates (for RFM #1)
    <n>R     use one of the possible data rates (for RFM #2)
    <n>t     0=no toggle, else interval in seconds (for RFM #1)
    <n>T     0=no toggle, else interval in seconds (for RFM #2)
       v     show version
       <n>y     if 1 all received packets will be retransmitted  (Relay mode)
"""

class LaCrosseGateway(object):

    sensors = {}
    _registry = {}
    _callback = None
    _socket = None
    _stopevent = None
    _thread = None

    def __init__(self, host, port):
        """Initialize the LacrosseGateway device."""
        self._host = host
        self._port = port
        self._callback_data = None

    def connect(self):
        """Connect to the device."""
        self._socket = socket.socket()
        self._socket.connect((self._host, self._port))

    def close(self):
        """Close the device."""
        self._stop_worker()
        self._socket.close()

    def start_scan(self):
        """Start scan task in background."""
        self._start_worker()

    def _write_cmd(self, cmd):
        """Write to socket."""
        self._socket.sendall((cmd + '\r\n').encode())

    @staticmethod
    def _parse_info(line):
        """
        The output can be:
        - [LaCrosseITPlusReader.10.1s (RFM12B f:0 r:17241)]
        - [LaCrosseITPlusReader.10.1s (RFM12B f:0 t:10~3)]
        - [LaCrosseITPlusReader.Gateway.1.35 (1=RFM69 f:868300 r:8) {IP=192.168.178.40}]
        """
        re_info = re.compile(
            r'\[(?P<name>\w+\.\w+).(?P<ver>.*) ' +
            r'\(1=(?P<rfm1name>\w+) (\w+):(?P<rfm1freq>\d+) ' +
            r'(?P<rfm1mode>.*)\) {IP=(?P<address>.*)}\]')

        info = {
            'name': None,
            'version': None,
            'address': None,
            'rfm1name': None,
            'rfm1frequency': None,
            'rfm1datarate': None,
            'rfm1toggleinterval': None,
            'rfm1togglemask': None,
        }
        match = re_info.match(line)
        if match:
            info['name'] = match.group('name')
            info['version'] = match.group('ver')
            info['address'] = match.group('address')
            info['rfm1name'] = match.group('rfm1name')
            info['rfm1frequency'] = match.group('rfm1freq')
            values = match.group('rfm1mode').split(':')
            if values[0] == 'r':
                info['rfm1datarate'] = values[1]
            elif values[0] == 't':
                toggle = values[1].split('~')
                info['rfm1toggleinterval'] = toggle[0]
                info['rfm1togglemask'] = toggle[1]

        return info

    def get_info(self):
        """Get current configuration info from 'v' command."""
        re_info = re.compile(r'\[.*\]')

        while True:
            self._write_cmd('v')

            for x in range(10):
                line = self._socket.recv(1024)            
                try:
                    line = line.encode().decode('utf-8').strip('\r\n')
                except AttributeError:
                    line = line.decode('utf-8').strip('\r\n')

                match = re_info.match(line)
                if match:
                    return self._parse_info(line)

    def led_mode_state(self, state):
        """Set the LED mode.

        The LED state can be True or False.
        """
        self._write_cmd('{}a'.format(int(state)))

    def set_frequency(self, frequency, rfm=1):
        """Set frequency in kHz.

        The frequency can be set in 5kHz steps.
        """
        cmds = {1: 'f', 2: 'F'}
        self._write_cmd('{}{}'.format(frequency, cmds[rfm]))

    def set_datarate(self, rate, rfm=1):
        """Set datarate (baudrate)."""
        cmds = {1: 'r', 2: 'R'}
        self._write_cmd('{}{}'.format(rate, cmds[rfm]))

    def set_toggle_interval(self, interval, rfm=1):
        """Set the toggle interval."""
        cmds = {1: 't', 2: 'T'}
        self._write_cmd('{}{}'.format(interval, cmds[rfm]))

    def set_toggle_mask(self, mode_mask, rfm=1):
        """Set toggle baudrate mask.

        The baudrate mask values are:
          1: 17.241 kbps
          2 : 9.579 kbps
          4 : 8.842 kbps
        These values can be or'ed.
        """
        cmds = {1: 'm', 2: 'M'}
        self._write_cmd('{}{}'.format(mode_mask, cmds[rfm]))

    def _start_worker(self):
        if self._thread is not None:
            return
        self._stopevent = threading.Event()
        self._thread = threading.Thread(target=self._refresh, args=())
        self._thread.daemon = True
        self._thread.start()

    def _stop_worker(self):
        if self._stopevent is not None:
            self._stopevent.set()
        if self._thread is not None:
            self._thread.join()

    def _refresh(self):
        """Background refreshing thread."""

        while not self._stopevent.isSet():
            line = self._socket.recv(1024)
            #this is for python2/python3 compatibility. Is there a better way?
            try:
                line = line.encode().decode('utf-8').strip('\r\n')
            except AttributeError:
                line = line.decode('utf-8').strip('\r\n')

            if LaCrosseSensor.re_reading.match(line):
                sensor = LaCrosseSensor(line)
                self.sensors[sensor.sensorid] = sensor

                if self._callback:
                    self._callback(sensor, self._callback_data)

                if sensor.sensorid in self._registry:
                    for cbs in self._registry[sensor.sensorid]:
                        cbs[0](sensor, cbs[1])

    def register_callback(self, sensorid, callback, user_data=None):
        """Register a callback for the specified sensor id."""
        if sensorid not in self._registry:
            self._registry[sensorid] = list()
        self._registry[sensorid].append((callback, user_data))

    def register_all(self, callback, user_data=None):
        """Register a callback for all sensors."""
        self._callback = callback
        self._callback_data = user_data


class LaCrosseSensor(object):
    """The LaCrosse Sensor class."""
    # OK 9 248 1 4 150 106
    # OK 22 121 49 3 222 240 0 1 82 87 121 0 4 148 225 0 0 38 229 1 0 [79 31 F0 00 00 00 57 79 00 00 00 00 6D A7 08 00 00 26 E5 00 08 40 09 A9 00 9D 40 0C 7D 3D E0 00 00 00 04 15 20 10 02 EF 17]
    re_reading = re.compile(r'OK (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)')

    def __init__(self, line=None):
        if line:
            self._parse(line)

    def _parse(self, line):
        match = self.re_reading.match(line)
        if match:
            data = [int(c) for c in match.group().split()[1:]]
            self.sensortype = data[0]
            self.sensorid = ''.join(f'{i:02X}' for i in [data[1], data[2]]) # str([f'{i:02x}' for i in [data[1], data[2]]])
            self.ontime = (data[3] * 16777216) + (data[4] * 65536) + (data[5] * 256) + data[6]
            self.totaltime = (data[7] * 16777216) + (data[8] * 65536) + (data[9] * 256) + data[10]
            self.energy = (data[11] * 16777216) + (data[12] * 65536) + (data[13] * 256) + data[14]
            self.power = (data[15] * 256) + data[16]
            self.maxpower = (data[17] * 256) + data[18]
            self.resets = data[19]

    def __repr__(self):
        return "id=%s pw=%d" % \
            (self.sensorid, self.power)
