# Copyright 2015 Adafruit Industries.
# Author: Tony DiCola
# License: GNU GPLv2, see LICENSE.txt
import os
import shutil
import subprocess
import tempfile
import time
import dbus
import RPi.GPIO as GPIO

from .alsa_config import parse_hw_device

class OMXPlayer:

    def __init__(self, config):
        """Create an instance of a video player that runs omxplayer in the
        background.
        """
        self._process = None
        self._temp_directory = None
        self._load_config(config)
        self._relay_state = None
        self._relay_on_pin = 5
        self._relay_off_pin = 3
        self._relay_pulse_width_s = 0.05
        GPIO.setmode(GPIO.BOARD)

    def __del__(self):
        if self._temp_directory:
            shutil.rmtree(self._temp_directory)

    def _get_temp_directory(self):
        if not self._temp_directory:
            self._temp_directory = tempfile.mkdtemp()
        return self._temp_directory

    def _load_config(self, config):
        self._extensions = config.get('omxplayer', 'extensions') \
                                 .translate(str.maketrans('', '', ' \t\r\n.')) \
                                 .split(',')
        self._extra_args = config.get('omxplayer', 'extra_args').split()
        self._sound = config.get('omxplayer', 'sound').lower()
        assert self._sound in ('hdmi', 'local', 'both', 'alsa'), 'Unknown omxplayer sound configuration value: {0} Expected hdmi, local, both or alsa.'.format(self._sound)
        self._alsa_hw_device = parse_hw_device(config.get('alsa', 'hw_device'))
        if self._alsa_hw_device != None and self._sound == 'alsa':
            self._sound = 'alsa:hw:{},{}'.format(self._alsa_hw_device[0], self._alsa_hw_device[1])
        self._show_titles = config.getboolean('omxplayer', 'show_titles')
        if self._show_titles:
            title_duration = config.getint('omxplayer', 'title_duration')
            if title_duration >= 0:
                m, s = divmod(title_duration, 60)
                h, m = divmod(m, 60)
                self._subtitle_header = '00:00:00,00 --> {:d}:{:02d}:{:02d},00\n'.format(h, m, s)
            else:
                self._subtitle_header = '00:00:00,00 --> 99:59:59,00\n'
        self._trigger_timestamp = config.get('trigger', 'timestamp').split()
        self._trigger_timestamp_len = len(self._trigger_timestamp)//2

    def supported_extensions(self):
        """Return list of supported file extensions."""
        return self._extensions

    def play(self, movie, loop=None, vol=0):
        """Play the provided movie file, optionally looping it repeatedly."""
        self.stop(3)  # Up to 3 second delay to let the old player stop.
        # Assemble list of arguments.
        args = ['omxplayer']
        args.extend(['-o', self._sound])  # Add sound arguments.
        args.extend(self._extra_args)     # Add extra arguments from config.
        if vol is not 0:
            args.extend(['--vol', str(vol)])
        if loop is None:
            loop = movie.repeats
        if loop <= -1:
            args.append('--loop')  # Add loop parameter if necessary.
        if self._show_titles and movie.title:
            srt_path = os.path.join(self._get_temp_directory(), 'video_looper.srt')
            with open(srt_path, 'w') as f:
                f.write(self._subtitle_header)
                f.write(movie.title)
            args.extend(['--subtitles', srt_path])
        args.append(movie.filename)       # Add movie file path.
        # Run omxplayer process and direct standard output to /dev/null.
        self._process = subprocess.Popen(args,
                                         stdout=open(os.devnull, 'wb'),
                                         close_fds=True)
        self._relay_state = False
        print ('Play Relay FORCE_OFF')
        GPIO.setup(self._relay_off_pin,GPIO.OUT)
        time.sleep(self._relay_pulse_width_s)
        GPIO.setup(self._relay_off_pin,GPIO.IN)

    def is_playing(self):
        """Return true if the video player is running, false otherwise."""
        if self._process is None:
            return False
        self._in_trigger_range = None
        try:
            with open('/tmp/omxplayerdbus.root', 'r+') as fd:
                sock_info = fd.read().strip()

            bus = dbus.bus.BusConnection(sock_info)
            obj = bus.get_object('org.mpris.MediaPlayer2.omxplayer',
                    '/org/mpris/MediaPlayer2', introspect=False)
            ifp = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
            
            for i in range(self._trigger_timestamp_len):
                _trigger_start = int(self._trigger_timestamp[i*2])
                _trigger_duration = int(self._trigger_timestamp[i*2+1])
                _trigger_stop = _trigger_start + _trigger_duration
                if ifp.Position() > _trigger_start and ifp.Position() < _trigger_stop and self._in_trigger_range != True:
                    self._in_trigger_range = True
                    break
                else:
                    self._in_trigger_range = False
        except:
            print('DBUS Not Created')
        if self._in_trigger_range == True:
            if self._relay_state != True:
                print ('Relay ON ',ifp.Position())
                self._relay_state = True
                GPIO.setup(self._relay_on_pin,GPIO.OUT)
                time.sleep(self._relay_pulse_width_s)
                GPIO.setup(self._relay_on_pin,GPIO.IN)
        else:
            if self._relay_state != False:
                print ('Relay OFF',ifp.Position())
                self._relay_state = False
                GPIO.setup(self._relay_off_pin,GPIO.OUT)
                time.sleep(self._relay_pulse_width_s)
                GPIO.setup(self._relay_off_pin,GPIO.IN)
        self._process.poll()
        return self._process.returncode is None

    def stop(self, block_timeout_sec=0):
        """Stop the video player.  block_timeout_sec is how many seconds to
        block waiting for the player to stop before moving on.
        """
        # Stop the player if it's running.
        if self._process is not None and self._process.returncode is None:
            # There are a couple processes used by omxplayer, so kill both
            # with a pkill command.
            subprocess.call(['pkill', '-9', 'omxplayer'])
        # If a blocking timeout was specified, wait up to that amount of time
        # for the process to stop.
        start = time.time()
        while self._process is not None and self._process.returncode is None:
            if (time.time() - start) >= block_timeout_sec:
                break
            time.sleep(0)
        # Let the process be garbage collected.
        self._process = None
        
        self._relay_state = False
        print ('Stop Relay FORCE_OFF')
        GPIO.setup(self._relay_off_pin,GPIO.OUT)
        time.sleep(self._relay_pulse_width_s)
        GPIO.setup(self._relay_off_pin,GPIO.IN)

    @staticmethod
    def can_loop_count():
        return False


def create_player(config):
    """Create new video player based on omxplayer."""
    return OMXPlayer(config)
