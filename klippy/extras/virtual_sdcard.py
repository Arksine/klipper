# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging

class VirtualSD:
    def __init__(self, config):
        printer = config.get_printer()
        printer.register_event_handler("klippy:shutdown", self.handle_shutdown)
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        # Print Stat Tracking
        self.print_stats = PrintStats(config)
        # File List/Info
        self.filemanager = DetailedFileList(config, self.sdcard_dirname)
        self.get_detailed_file_list = self.filemanager.get_file_list
        # Work timer
        self.reactor = printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.work_timer = None
        # Register commands
        self.gcode = printer.lookup_object('gcode')
        self.gcode.register_command('M21', None)
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "RESET_SD", self.cmd_RESET_SD,
            desc=self.cmd_RESET_SD_help)
        self.gcode.register_command(
            "GET_SD_STATUS", self.cmd_GET_SD_STATUS,
            desc=self.cmd_GET_SD_STATUS_help)
        # Register Webhooks
        webhooks = printer.lookup_object('webhooks')
        webhooks.register_endpoint(
            "/printer/print/start", self._handle_remote_start_request,
            methods=["POST"])
        webhooks.register_endpoint(
            "/printer/files", self._handle_remote_filelist_request)
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def _handle_remote_start_request(self, web_request):
        filename = web_request.get('filename')
        if filename[0] != '/':
            filename = '/' + filename
        script = "M23 " + filename + "\nM24"
        web_request.put('script', script)
        self.gcode.run_script_from_remote(web_request)
    def _handle_remote_filelist_request(self, web_request):
        try:
            filelist = self.get_detailed_file_list()
        except Exception:
            raise web_request.error("Unable to retreive file list")
        flist = []
        for fname in sorted(filelist, key=str.lower):
            fdict = {'filename': fname}
            fdict.update(filelist[fname])
            flist.append(fdict)
        web_request.send(flist)
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self):
        filenames = self.filemanager.get_file_list()
        return [(fname, filenames[fname]['size'])
                for fname in sorted(filenames, key=str.lower)]
    def get_status(self, eventtime):
        progress = 0.
        if self.file_size:
            progress = float(self.file_position) / self.file_size
        is_active = self.is_active()
        status = self.print_stats.get_status(eventtime)
        status.update({'progress': progress, 'is_active': is_active,
                       'file_position': self.file_position})
        return status
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
    # G-Code commands
    def cmd_error(self, params):
        raise self.gcode.error("SD write not supported")
    cmd_GET_SD_STATUS_help = "Show Virtual SD Print Details"
    def cmd_GET_SD_STATUS(self, params):
        curtime = self.reactor.monotonic()
        sd_status = self.get_status(curtime)
        self.gcode.respond_info(
            "SD active: %s\nfile name: %s\nprogress: %.2f%%\n"
            "time elapsed since start: %.2f s\nprint duration: %.2f s\n"
            "filament used: %.2f mm\nfile position: %d" %
            (str(sd_status['is_active']), sd_status['current_file'],
             sd_status['progress'] * 100., sd_status['total_duration'],
             sd_status['print_duration'], sd_status['filament_used'],
             self.file_position))
    cmd_RESET_SD_help = "Clears a loaded SD File. Stops the print if necessary"
    def cmd_RESET_SD(self, params):
        if self.cmd_from_sd:
            raise self.gcode.error(
                "RESET_SD cannot be run from the sdcard")
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.file_position = self.file_size = 0.
        self.print_stats.reset()
    def cmd_M20(self, params):
        # List SD card
        files = self.get_file_list()
        self.gcode.respond("Begin file list")
        for fname, fsize in files:
            self.gcode.respond("%s %d" % (fname, fsize))
        self.gcode.respond("End file list")
    def cmd_M21(self, params):
        # Initialize SD card
        self.gcode.respond("SD card ok")
    def cmd_M23(self, params):
        # Select SD file
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        if self.current_file is not None:
            self.current_file.close()
            self.current_file = None
            self.file_position = self.file_size = 0
            self.print_stats.reset()
        try:
            orig = params['#original']
            filename = orig[orig.find("M23") + 4:].split()[0].strip()
            if '*' in filename:
                filename = filename[:filename.find('*')].strip()
        except:
            raise self.gcode.error("Unable to extract filename")
        if filename.startswith('/'):
            filename = filename[1:]
        files = self.get_file_list()
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        try:
            fname = files_by_lower[filename.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = open(fname, 'rb')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except:
            logging.exception("virtual_sdcard file open")
            raise self.gcode.error("Unable to open file")
        self.gcode.respond("File opened:%s Size:%d" % (filename, fsize))
        self.gcode.respond("File selected")
        self.current_file = f
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
    def cmd_M24(self, params):
        # Start/resume SD print
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def cmd_M25(self, params):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, params):
        # Set SD position
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        pos = self.gcode.get_int('S', params, minval=0)
        self.file_position = pos
    def cmd_M27(self, params):
        # Report SD print status
        if self.current_file is None:
            self.gcode.respond("Not SD printing.")
            return
        self.gcode.respond("SD printing byte %d/%d" % (
            self.file_position, self.file_size))
    # Background work timer
    def work_handler(self, eventtime):
        logging.info("Starting SD card print (position %d)", self.file_position)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.gcode.respond_error("Unable to seek file")
            self.work_timer = None
            return self.reactor.NEVER
        self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self.current_file.read(8192)
                except:
                    logging.exception("virtual_sdcard read")
                    self.gcode.respond_error("Error on virtual sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    logging.info("Finished SD card print")
                    self.gcode.respond("Done printing file")
                    break
                lines = data.split('\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            try:
                self.gcode.run_script(lines[-1])
            except self.gcode.error as e:
                break
            except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position += len(lines.pop()) + 1
        logging.info("Exiting SD card print (position %d)", self.file_position)
        self.work_timer = None
        self.cmd_from_sd = False
        if self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.file_position = self.file_size = 0
            self.print_stats.reset()
        return self.reactor.NEVER

class PrintStats:
    def __init__(self, config):
        printer = config.get_printer()
        self.gcode = printer.lookup_object('gcode')
        self.reactor = printer.get_reactor()
        self.reset()
    def _update_filament_usage(self, eventtime):
        gc_status = self.gcode.get_status(eventtime)
        cur_epos = gc_status['last_epos']
        self.filament_used += (cur_epos - self.last_epos) \
            / gc_status['extrude_factor']
        self.last_epos = cur_epos
    def set_current_file(self, filename):
        self.reset()
        self.filename = filename
    def note_start(self):
        curtime = self.reactor.monotonic()
        if self.print_start_time is None:
            self.print_start_time = curtime
        elif self.last_pause_time is not None:
            # Update pause time duration
            pause_duration = curtime - self.last_pause_time
            self.prev_pause_duration += pause_duration
            self.last_pause_time = None
        # Reset last e-position
        gc_status = self.gcode.get_status(curtime)
        self.last_epos = gc_status['last_epos']
    def note_pause(self):
        if self.last_pause_time is None:
            curtime = self.reactor.monotonic()
            self.last_pause_time = curtime
            # update filament usage
            self._update_filament_usage(curtime)
    def reset(self):
        self.filename = ""
        self.prev_pause_duration = self.last_epos = 0.
        self.filament_used = 0.
        self.print_start_time = self.last_pause_time = None
    def get_status(self, eventtime):
        total_duration = 0.
        time_paused = self.prev_pause_duration
        if self.print_start_time is not None:
            curtime = self.reactor.monotonic()
            if self.last_pause_time is not None:
                # Calculate the total time spent paused during the print
                time_paused += curtime - self.last_pause_time
            else:
                # Accumulate filament if not paused
                self._update_filament_usage(curtime)
            total_duration = curtime - self.print_start_time
        return {
            'current_file': self.filename,
            'total_duration': total_duration,
            'print_duration': total_duration - time_paused,
            'filament_used': self.filament_used
        }

class DetailedFileList:
    def __init__(self, config, sd_path):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.gca = self.printer.try_load_module(config, 'gcode_analysis')
        self.sd_path = sd_path
        self.file_info = None
        self.gcode.register_command(
            "GET_SD_FILE_LIST", self.cmd_GET_SD_FILE_LIST,
            desc=self.cmd_GET_SD_FILE_LIST_help)
        self.update_file_list()
    def update_file_list(self):
        file_names = self._get_file_names(self.sd_path)
        if self.file_info is None:
            self.file_info = {}
            for fname in file_names:
                fpath = os.path.join(self.sd_path, fname)
                try:
                    self.file_info[fname] = self.gca.get_metadata(fpath)
                except Exception:
                    logging.exception("virtual_sdcard: File Detection Error")
        else:
            new_file_info = {}
            for fname in file_names:
                fpath = os.path.join(self.sd_path, fname)
                metadata = self.file_info.get(fname, None)
                try:
                    if metadata is not None:
                        new_file_info[fname] = self.gca.update_metadata(
                            fpath, metadata)
                    else:
                        new_file_info[fname] = self.gca.get_metadata(fpath)
                except Exception:
                    logging.exception("virtual_sdcard: File Detection Error")
            self.file_info = new_file_info
    def get_file_list(self):
        self.update_file_list()
        return dict(self.file_info)
    def _get_file_names(self, path):
        file_names = []
        try:
            for fname in os.listdir(path):
                if fname.startswith('.'):
                    continue
                full_path = os.path.join(path, fname)
                if os.path.isdir(full_path):
                    sublist = self._get_file_names(full_path)
                    for sf in sublist:
                        file_names.append(os.path.join(fname, sf))
                elif os.path.isfile(full_path):
                    file_names.append(fname)
        except Exception:
            msg = "virtual_sdcard: unable to generate file list"
            logging.exception(msg)
            if self.file_info is None:
                # File Info not initialized, error occured during config
                raise self.printer.config_error(msg)
            else:
                raise self.gcode.error(msg)
        return file_names
    cmd_GET_SD_FILE_LIST_help = "Show Detailed VSD File Information"
    def cmd_GET_SD_FILE_LIST(self, params):
        self.update_file_list()
        msg = "Available Virtual SD Files:\n"
        for fname in sorted(self.file_info, key=str.lower):
            msg += "File: %s\n" % (fname)
            for item in sorted(self.file_info[fname], key=str.lower):
                msg += "** %s: %s\n" % (item, str(self.file_info[fname][item]))
            msg += "\n"
        self.gcode.respond_info(msg)

def load_config(config):
    return VirtualSD(config)
