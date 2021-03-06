"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor. If not, see <http://www.gnu.org/licenses/>.
"""

try:
    import pgi
    pgi.install_as_gi()
except ImportError:
    pass
    
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib

import locale
import mlt
import os
import subprocess
import sys
import threading
import time

import appconsts
import atomicfile
import editorstate
import editorpersistance
import gmicplayer
import mltfilters
import mltenv
import mltprofiles
import mlttransitions
import processutils
import renderconsumer
import respaths
import translations
import userfolders

CLIP_FRAMES_DIR = "/clip_frames"
RENDERED_FRAMES_DIR = "/rendered_frames"

COMPLETED_MSG_FILE = "completed"
STATUS_MSG_FILE = "status"

_gmic_version = None

_session_folder = None
_clip_frames_folder = None
_rendered_frames_folder = None

_render_thread = None


# ----------------------------------------------------- module interface with message files
# We are using message files to communicate with application.
def clear_flag_files(session_id):
    folder = _get_session_folder(session_id)
    
    completed_msg = folder + "/" + COMPLETED_MSG_FILE
    if os.path.exists(completed_msg):
        os.remove(completed_msg)

    status_msg_file = folder + "/" + STATUS_MSG_FILE
    if os.path.exists(status_msg_file):
        os.remove(status_msg_file)

def session_render_complete(session_id):
    folder = _get_session_folder(session_id)
    completed_msg = folder + "/" + COMPLETED_MSG_FILE
    if os.path.exists(completed_msg):
        return True
    else:
        return False

def get_session_status(session_id):
    try:
        status_msg_file = _get_session_folder(session_id) + "/" + STATUS_MSG_FILE
        with open(status_msg_file) as f:
            msg = f.read()
    except:
        return None
        
    step, frame, length, elapsed = msg.split(" ")
    return (step, frame, length, elapsed)
    
        
def _get_session_folder(session_id):
    return userfolders.get_data_dir() + appconsts.CONTAINER_CLIPS_DIR +  "/" + session_id


# --------------------------------------------------- render thread launch
def main(root_path, session_id, script, clip_path, range_in, range_out, profile_desc):
    
    os.nice(10) # make user configurable
    
    prints_to_log_file("/home/janne/gmicheadless")
    print(session_id, script, clip_path, range_in, range_out, profile_desc)

    try:
        editorstate.mlt_version = mlt.LIBMLT_VERSION
    except:
        editorstate.mlt_version = "0.0.99" # magic string for "not found"

    # Set paths.
    respaths.set_paths(root_path)

    # Check G'MIC version
    global _gmic_version
    _gmic_version = get_gmic_version()
    if _gmic_version == 2:
        respaths.set_gmic2(root_path)

    userfolders.init()
    editorpersistance.load()

    # Init translations module with translations data
    translations.init_languages()
    translations.load_filters_translations()
    mlttransitions.init_module()

    repo = mlt.Factory().init()
    processutils.prepare_mlt_repo(repo)
    
    # Set numeric locale to use "." as radix, MLT initilizes this to OS locale and this causes bugs 
    locale.setlocale(locale.LC_NUMERIC, 'C')

    # Check for codecs and formats on the system
    mltenv.check_available_features(repo)
    renderconsumer.load_render_profiles()

    # Load filter and compositor descriptions from xml files.
    mltfilters.load_filters_xml(mltenv.services)
    mlttransitions.load_compositors_xml(mltenv.transitions)

    # Create list of available mlt profiles
    mltprofiles.load_profile_list()
    global _session_folder, _clip_frames_folder, _rendered_frames_folder
    _session_folder = _get_session_folder(session_id)
    _clip_frames_folder = _session_folder + CLIP_FRAMES_DIR
    _rendered_frames_folder = _session_folder + RENDERED_FRAMES_DIR

    # Init gmic session dirs, these might exist if clip has been rendered before
    if not os.path.exists(_session_folder):
        os.mkdir(_session_folder)
    if not os.path.exists(_clip_frames_folder):
        os.mkdir(_clip_frames_folder)
    if not os.path.exists(_rendered_frames_folder):
        os.mkdir(_rendered_frames_folder)
            
    global _render_thread
    _render_thread = GMicHeadlessRunnerThread(script, clip_path, range_in, range_out, profile_desc)
    _render_thread.start()

def get_gmic_version():
    gmic_ver = 1
    cmd = "gmic -version"
    process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
    output, error = process.communicate()
    tokens = output.split()
    clended = []
    for token in tokens:
        token = token.decode("utf-8")
        str1 = token.replace('.','')
        str2 = str1.replace(',','')
        if str2.isdigit(): # this is based on assumtion that str2 ends up being number like "175" or 215" etc. only for version number token
            if str2[0] == '2':
                gmic_ver = 2

    return gmic_ver

        

class GMicHeadlessRunnerThread(threading.Thread):

    def __init__(self, script, clip_path, range_in, range_out, profile_desc):
        threading.Thread.__init__(self)

        self.script_path = script
        self.clip_path = clip_path
        self.range_in = int(range_in)
        self.range_out = int(range_out)
        self.length = self.range_out - self.range_in + 1
        self.profile_desc = profile_desc
        
        self.aborted = False
        self.last_frame_out_update = -1
        
    def run(self):
        self.start_time = time.monotonic()
        
        self.render_player = None
        self.frames_range_writer = None
        
        self.abort = False
        self.script_renderer = None
       
        frame_name = "frame"
        profile = mltprofiles.get_profile(self.profile_desc)

        # Delete old preview frames
        for frame_file in os.listdir(_clip_frames_folder):
            file_path = os.path.join(_clip_frames_folder, frame_file)
            os.remove(file_path)
            
        self.frames_range_writer = gmicplayer.FramesRangeWriter(self.clip_path, self.frames_update, profile)
        self.frames_range_writer.write_frames(_clip_frames_folder + "/", frame_name, self.range_in, self.range_out)

        if self.abort == True:
            return

        script_file = open(self.script_path)
        user_script = script_file.read()
        print(user_script)
        while len(os.listdir(_clip_frames_folder)) != self.length:
            print("WAITING")
            time.sleep(0.5)
        
        # Render frames with gmic script
        self.script_renderer = gmicplayer.FolderFramesScriptRenderer(   user_script, 
                                                                        _clip_frames_folder,
                                                                        _rendered_frames_folder + "/",
                                                                        frame_name,
                                                                        self.script_render_update_callback, 
                                                                        self.script_render_output_callback,
                                                                        nice=10,
                                                                        re_render_existing=False)
        self.script_renderer.write_frames()

        # Write out completed flag file.
        completed_msg_file =_session_folder + "/" + COMPLETED_MSG_FILE
        script_text = "##completed##" # let's put something in here
        with atomicfile.AtomicFileWriter(completed_msg_file, "w") as afw:
            script_file = afw.get_file()
            script_file.write(script_text)

    def frames_update(self, frame):
        # step 1, frame , range
        elapsed = time.monotonic() - self.start_time
        msg = "1 " + str(frame) + " " + str(self.length) + " " + str(elapsed)
        self.write_status_message(msg)
        
    def script_render_update_callback(self, frame_count):
        # step 1, frame , range
        elapsed = time.monotonic() - self.start_time
        msg = "2 " + str(frame_count) + " " + str(self.length) + " " + str(elapsed)
        self.write_status_message(msg)

    def write_status_message(self, msg):
        try:
            status_msg_file = _session_folder + "/" + STATUS_MSG_FILE
            
            with atomicfile.AtomicFileWriter(status_msg_file, "w") as afw:
                script_file = afw.get_file()
                script_file.write(msg)
        except:
            pass # this failing because we can get file access will show as progress hickup to user, we don't care
            
    def script_render_output_callback(self, p, out):
        if p.returncode != 0:
            # TODO: handling errors.
            pass


# ---- Debug helper
def prints_to_log_file(log_file):
    so = se = open(log_file, 'w', buffering=1)

    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)

    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())
        
