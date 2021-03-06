#!/usr/bin/env python

#***********************************************************
#* Software License Agreement (BSD License)
#*
#*  Copyright (c) 2009, Willow Garage, Inc.
#*  All rights reserved.
#*
#*  Redistribution and use in source and binary forms, with or without
#*  modification, are permitted provided that the following conditions
#*  are met:
#*
#*   * Redistributions of source code must retain the above copyright
#*     notice, this list of conditions and the following disclaimer.
#*   * Redistributions in binary form must reproduce the above
#*     copyright notice, this list of conditions and the following
#*     disclaimer in the documentation and/or other materials provided
#*     with the distribution.
#*   * Neither the name of the Willow Garage nor the names of its
#*     contributors may be used to endorse or promote products derived
#*     from this software without specific prior written permission.
#*
#*  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#*  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#*  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
#*  FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
#*  COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
#*  INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
#*  BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#*  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#*  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
#*  LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
#*  ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#*  POSSIBILITY OF SUCH DAMAGE.
#***********************************************************

# Author: Blaise Gassend

import roslib
import rospy
import threading
import os
import logging
import sys
import traceback
import tempfile
from diagnostic_msgs.msg import DiagnosticStatus, KeyValue, DiagnosticArray
from sound_play.msg import SoundRequest, SoundRequestAction, SoundRequestResult, SoundRequestFeedback

from multiprocessing import Queue, Value
from ctypes import c_bool

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst as Gst
    from gi.repository import GObject as GObject
except:
    str="""
**************************************************************
Error opening pygst. Is gstreamer installed?
**************************************************************
"""
    rospy.logfatal(str)
    # print str
    exit(1)


def sleep(t):
    try:
        rospy.sleep(t)
    except:
        pass


class soundtype:
    STOPPED = 0
    LOOPING = 1
    COUNTING = 2

    _sound_say_started = Value(c_bool, False)

    def __init__(self, file, device, volume = 1.0):
        self.lock = threading.RLock()
        self.state = self.STOPPED
        self.sound = Gst.ElementFactory.make("playbin",None)
        if self.sound is None:
            raise Exception("Could not create sound player")

        if device:
            self.sink = Gst.ElementFactory.make("alsasink", "sink")
            self.sink.set_property("device", device)
            self.sound.set_property("audio-sink", self.sink)

        if (":" in file):
            uri = file
        elif os.path.isfile(file):
            uri = "file://" + os.path.abspath(file)
        else:
          rospy.logerr('Error: URI is invalid: %s'%file)

        self.uri = uri
        self.volume = volume
        self.sound.set_property('uri', uri)
        self.sound.set_property("volume",volume)
        self.staleness = 1
        self.file = file

        self.bus = self.sound.get_bus()
        self.bus.add_signal_watch()
        self.bus_conn_id = self.bus.connect("message", self.on_stream_end)

    def on_stream_end(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            if (self.state == self.LOOPING):
                self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            else:
                self.stop()

    def __del__(self):
        # stop our GST object so that it gets garbage-collected
        self.dispose()

    def update(self):
        if self.bus is not None:
            self.bus.poll(Gst.MessageType.ERROR, 10)

    def loop(self):
        self.lock.acquire()
        try:
            self.staleness = 0
            if self.state == self.COUNTING:
                self.stop()

            if self.state == self.STOPPED:
              self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
              self.sound.set_state(Gst.State.PLAYING)
            self.state = self.LOOPING
        finally:
            self.lock.release()

    def dispose(self):
        self.lock.acquire()
        try:
            if self.bus is not None:
                self.sound.set_state(Gst.State.NULL)
                self.bus.disconnect(self.bus_conn_id)
                self.bus.remove_signal_watch()
                self.bus = None
                self.sound = None
                self.sink = None
                self.state = self.STOPPED
        except Exception as e:
            rospy.logerr('Exception in dispose: %s'%str(e))
        finally:
            self.lock.release()

    def stop(self):
        if self.state != self.STOPPED:
            self.lock.acquire()
            try:
                self.sound.set_state(Gst.State.NULL)
                self.state = self.STOPPED
            finally:
                self.lock.release()

    def single(self):
        self.lock.acquire()
        try:
            rospy.logdebug("Playing %s"%self.uri)
            self.staleness = 0
            self._sound_say_started.value = False
            if self.state == self.LOOPING:
                self.stop()

            self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            self.sound.set_state(Gst.State.PLAYING)
            self.state = self.COUNTING
        finally:
            self.lock.release()

    def command(self, cmd):
         if cmd == SoundRequest.PLAY_STOP:
             self.stop()
         elif cmd == SoundRequest.PLAY_ONCE:
             self.single()
         elif cmd == SoundRequest.PLAY_START:
             self.loop()

    def get_staleness(self, check_playing=False):
        self.lock.acquire()
        position = 0
        duration = 0
        staleness = 0
        try:
            position = self.sound.query_position(Gst.Format.TIME)[1]
            duration = self.sound.query_duration(Gst.Format.TIME)[1]
        except Exception as e:
            position = 0
            duration = 0
        finally:
            self.lock.release()

        if position != duration:
            staleness = 0

        else:
            staleness = self.staleness + 1
                    
        if  self._sound_say_started.value is False and duration == -1:
            staleness = 0
        elif self._sound_say_started.value is False:
            self._sound_say_started.value = True
        
        if check_playing is False:
            self.staleness = staleness
        return staleness

    def get_playing(self):
        return self.state == self.COUNTING

class soundplay:
    _feedback = SoundRequestFeedback()
    _result   = SoundRequestResult()

    _queues_to_say = [Queue(), Queue(), Queue()]
    _queues_to_say_busy = Value(c_bool, False)

    _last_sound_say = None
    _sound_say_busy = Value(c_bool, False)

    def stopdict(self,dict):
        for sound in dict.values():
            sound.stop()

    def stopall(self):
        self.stopdict(self.builtinsounds)
        self.stopdict(self.filesounds)
        self.stopdict(self.voicesounds)

    def select_sound(self, data):
        if data.sound == SoundRequest.PLAY_FILE:
            if not data.arg2:
                if not data.arg in self.filesounds.keys():
                    rospy.logdebug('command for uncached wave: "%s"'%data.arg)
                    try:
                        self.filesounds[data.arg] = soundtype(data.arg, self.device, data.volume)
                    except:
                        rospy.logerr('Error setting up to play "%s". Does this file exist on the machine on which sound_play is running?'%data.arg)
                        return
                else:
                    rospy.logdebug('command for cached wave: "%s"'%data.arg)
                    if self.filesounds[data.arg].sound.get_property('volume') != data.volume:
                        rospy.logdebug('volume for cached wave has changed, resetting volume')
                        self.filesounds[data.arg].sound.set_property('volume', data.volume)
                sound = self.filesounds[data.arg]
            else:
                absfilename = os.path.join(roslib.packages.get_pkg_dir(data.arg2), data.arg)
                if not absfilename in self.filesounds.keys():
                    rospy.logdebug('command for uncached wave: "%s"'%absfilename)
                    try:
                        self.filesounds[absfilename] = soundtype(absfilename, self.device, data.volume)
                    except:
                        rospy.logerr('Error setting up to play "%s" from package "%s". Does this file exist on the machine on which sound_play is running?'%(data.arg, data.arg2))
                        return
                else:
                    rospy.logdebug('command for cached wave: "%s"'%absfilename)
                    if self.filesounds[absfilename].sound.get_property('volume') != data.volume:
                        rospy.logdebug('volume for cached wave has changed, resetting volume')
                        self.filesounds[absfilename].sound.set_property('volume', data.volume)
                sound = self.filesounds[absfilename]
        elif data.sound == SoundRequest.SAY:
            if data.command == SoundRequest.PLAY_STOP:
                self._loading_speaking_command(data)
            else:
                self._add_to_queue_to_say(data)
            sound = None
        else:
            rospy.logdebug('command for builtin wave: %i'%data.sound)
            if data.sound not in self.builtinsounds or (data.sound in self.builtinsounds and data.volume != self.builtinsounds[data.sound].volume):
                params = self.builtinsoundparams[data.sound]
                volume = data.volume
                if params[1] != 1: # use the second param as a scaling for the input volume
                    volume = (volume + params[1])/2
                self.builtinsounds[data.sound] = soundtype(params[0], self.device, volume)
            sound = self.builtinsounds[data.sound]
        if sound is not None and \
                sound.staleness != 0 and data.command != SoundRequest.PLAY_STOP:
            # This sound isn't counted in active_sounds
            rospy.logdebug("activating %i %s"%(data.sound,data.arg))
            self.active_sounds = self.active_sounds + 1
            sound.staleness = 0
            #                    if self.active_sounds > self.num_channels:
            #                        mixer.set_num_channels(self.active_sounds)
            #                        self.num_channels = self.active_sounds
        return sound

    def _add_to_queue_to_say(self, data):
        try:
            self._queues_to_say[data.priority].put(data)
        except Exception as e:
            rospy.logerr("Exception in _add_to_queue_to_say: " + str(e) +
                            "\nMaybe invalid priority level: " + str(data.priority))

    def _say_from_queue(self):
        if self._sound_say_busy.value is False:
            data = None
            if not self._queues_to_say[SoundRequest.PRIORITY_THREE].empty():
                data = self._queues_to_say[SoundRequest.PRIORITY_THREE].get()
            
            elif not self._queues_to_say[SoundRequest.PRIORITY_TWO].empty():
                data = self._queues_to_say[SoundRequest.PRIORITY_TWO].get()
            
            elif not self._queues_to_say[SoundRequest.PRIORITY_ONE].empty():
                data = self._queues_to_say[SoundRequest.PRIORITY_ONE].get()
            
            if data is not None:
                self._sound_say_busy.value = True     
                # No need to check the lock as it is an atomic action

                sound_say = self._loading_speaking_command(data)
                sound_say.command(data.command)
                self._last_sound_say = sound_say

    def _end_phrase_check(self):
        if self._sound_say_busy.value is True and \
                self._last_sound_say.get_staleness(True) != 0:
            self._sound_say_busy.value = False

    def _loading_speaking_command(self, data):
        if not data.arg in self.voicesounds.keys():
            rospy.logdebug('command for uncached text: "%s"' % data.arg)
            txtfile = tempfile.NamedTemporaryFile(prefix='sound_play', suffix='.txt')
            (wavfile,wavfilename) = tempfile.mkstemp(prefix='sound_play', suffix='.wav')
            txtfilename=txtfile.name
            os.close(wavfile)
            voice = data.arg2
            try:
                try:
                    txtfile.write(data.arg.decode('UTF-8').encode('ISO-8859-15'))
                except UnicodeEncodeError:
                    txtfile.write(data.arg)
                txtfile.flush()
                os.system("text2wave -eval '("+voice+")' "+txtfilename+" -o "+wavfilename)
                try:
                    if os.stat(wavfilename).st_size == 0:
                        raise OSError # So we hit the same catch block
                except OSError:
                    rospy.logerr('Sound synthesis failed. Is festival installed? Is a festival voice installed? Try running "rosdep satisfy sound_play|sh". Refer to http://wiki.ros.org/sound_play/Troubleshooting')
                    return
                self.voicesounds[data.arg] = soundtype(wavfilename, self.device, data.volume)
            finally:
                txtfile.close()
        else:
            rospy.logdebug('command for cached text: "%s"'%data.arg)
            if self.voicesounds[data.arg].sound.get_property('volume') != data.volume:
                rospy.logdebug('volume for cached text has changed, resetting volume')
                self.voicesounds[data.arg].sound.set_property('volume', data.volume)
        sound = self.voicesounds[data.arg]
        return sound
        
    def callback(self,data):
        if not self.initialized:
            return
        self.mutex.acquire()

        try:
            if data.sound == SoundRequest.ALL and data.command == SoundRequest.PLAY_STOP:
                self.stopall()
            else:
                sound = self.select_sound(data)
                if data.sound != SoundRequest.SAY or \
                        data.command == SoundRequest.PLAY_STOP:
                    sound.command(data.command)
        except Exception as e:
            rospy.logerr('Exception in callback: %s'%str(e))
            rospy.loginfo(traceback.format_exc())
        finally:
            self.mutex.release()
            rospy.logdebug("done callback")

    # Purge sounds that haven't been played in a while.
    def cleanupdict(self, dict):
        purgelist = []
        for (key,sound) in dict.iteritems():
            try:
                staleness = sound.get_staleness()
            except Exception as e:
                rospy.logerr('Exception in cleanupdict for sound (%s): %s'%(str(key),str(e)))
                staleness = 100 # Something is wrong. Let's purge and try again.
            #print "%s %i"%(key, staleness)
            if staleness >= 10:
                purgelist.append(key)
            if staleness == 0: # Sound is playing
                self.active_sounds = self.active_sounds + 1
        for key in purgelist:
            rospy.logdebug('Purging %s from cache'%key)
            if dict[key].file[0:4] == "/tmp":
                os.remove(dict[key].file) 
                rospy.logdebug("Remove " + dict[key].file)
            dict[key].dispose() # clean up resources
            del dict[key]

    def cleanup(self):
        self.mutex.acquire()
        try:
            self.active_sounds = 0
            self.cleanupdict(self.filesounds)
            self.cleanupdict(self.voicesounds)
            self.cleanupdict(self.builtinsounds)
        except:
            rospy.loginfo('Exception in cleanup: %s'%sys.exc_info()[0])
        finally:
            self.mutex.release()

    def diagnostics(self, state):
        try:
            da = DiagnosticArray()
            ds = DiagnosticStatus()
            ds.name = rospy.get_caller_id().lstrip('/') + ": Node State"
            if state == 0:
                ds.level = DiagnosticStatus.OK
                ds.message = "%i sounds playing"%self.active_sounds
                ds.values.append(KeyValue("Active sounds", str(self.active_sounds)))
                ds.values.append(KeyValue("Allocated sound channels", str(self.num_channels)))
                ds.values.append(KeyValue("Buffered builtin sounds", str(len(self.builtinsounds))))
                ds.values.append(KeyValue("Buffered wave sounds", str(len(self.filesounds))))
                ds.values.append(KeyValue("Buffered voice sounds", str(len(self.voicesounds))))
            elif state == 1:
                ds.level = DiagnosticStatus.WARN
                ds.message = "Sound device not open yet."
            else:
                ds.level = DiagnosticStatus.ERROR
                ds.message = "Can't open sound device. See http://wiki.ros.org/sound_play/Troubleshooting"
            da.status.append(ds)
            da.header.stamp = rospy.get_rostime()
            self.diagnostic_pub.publish(da)
        except Exception as e:
            rospy.loginfo('Exception in diagnostics: %s'%str(e))

    def __init__(self):
        Gst.init(None)


        # Start gobject thread to receive gstreamer messages
        GObject.threads_init()
        self.g_loop = threading.Thread(target=GObject.MainLoop().run)
        self.g_loop.daemon = True
        self.g_loop.start()


        rospy.init_node('sound_play')
        self.device = rospy.get_param("~device", "default")
        self.diagnostic_pub = rospy.Publisher("/diagnostics", DiagnosticArray, queue_size=1)
        rootdir = os.path.join(roslib.packages.get_pkg_dir('sound_play'),'sounds')

        self.builtinsoundparams = {
                SoundRequest.BACKINGUP              : (os.path.join(rootdir, 'BACKINGUP.ogg'), 0.1),
                SoundRequest.NEEDS_UNPLUGGING       : (os.path.join(rootdir, 'NEEDS_UNPLUGGING.ogg'), 1),
                SoundRequest.NEEDS_PLUGGING         : (os.path.join(rootdir, 'NEEDS_PLUGGING.ogg'), 1),
                SoundRequest.NEEDS_UNPLUGGING_BADLY : (os.path.join(rootdir, 'NEEDS_UNPLUGGING_BADLY.ogg'), 1),
                SoundRequest.NEEDS_PLUGGING_BADLY   : (os.path.join(rootdir, 'NEEDS_PLUGGING_BADLY.ogg'), 1),
                }

        self.no_error = True
        self.initialized = False
        self.active_sounds = 0

        self.mutex = threading.Lock()
        sub = rospy.Subscriber("robotsound", SoundRequest, self.callback)
        # self._as = actionlib.SimpleActionServer('sound_play', SoundRequestAction, execute_cb=self.execute_cb, auto_start = False)
        # self._as.start()

        self.mutex.acquire()
        self.sleep(0.5) # For ros startup race condition
        self.diagnostics(1)

        while not rospy.is_shutdown():
            while not rospy.is_shutdown():
                self.init_vars()
                self.no_error = True
                self.initialized = True
                self.mutex.release()
                try:
                    self.idle_loop()
                    # Returns after inactive period to test device availability
                    #print "Exiting idle"
                except:
                    rospy.loginfo('Exception in idle_loop: %s'%sys.exc_info()[0])
                finally:
                    self.mutex.acquire()

            self.diagnostics(2)
        self.mutex.release()

    def init_vars(self):
        self.num_channels = 10
        self.builtinsounds = {}
        self.filesounds = {}
        self.voicesounds = {}
        self.hotlist = []
        if not self.initialized:
            rospy.loginfo('sound_play node is ready to play sound')

    def sleep(self, duration):
        try:
            rospy.sleep(duration)
        except rospy.exceptions.ROSInterruptException:
            pass

    def idle_loop(self):
        self.last_activity_time = rospy.get_time()
        while (rospy.get_time() - self.last_activity_time < 10 or
                 len(self.builtinsounds) + len(self.voicesounds) + len(self.filesounds) > 0) \
                and not rospy.is_shutdown():
            self.diagnostics(0)
            self._say_phrase_with_delay_one_sec(20)
            self.cleanup()

    def _say_phrase_with_delay_one_sec(self, hz):
        one_delay = 1.0 / hz
        for i in range(hz):
            self._end_phrase_check()
            self._say_from_queue()
            self.sleep(one_delay)

if __name__ == '__main__':
    soundplay()
