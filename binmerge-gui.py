#!/usr/bin/python3
#
# binmerge-gui, a tool for merging, with a convenient GUI, a
# multi-track bin/wav disc image into a convenient bin/cue.
#
# (C) 2023 Gianluigi Cusimano, based on Chris Putnam's binmerge.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import datetime
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog
from tkinter import messagebox
import tkinter.scrolledtext as tkscrolled
import gettext
import locale
import pathlib
import re, os


def get_ui_language():
    try:
        from ctypes import windll
        lang_id = windll.kernel32.GetUserDefaultUILanguage()
        return [locale.windows_locale[lang_id]]
    except ImportError:
        pass

    current_locale, encoding = locale.getlocale(locale.LC_CTYPE)
    return [current_locale]


localedir = pathlib.Path(__file__).parent / pathlib.Path('locales')
lang = gettext.translation('base', localedir.resolve(), get_ui_language(), fallback=True)
lang.install()
_ = lang.gettext

VERBOSE = True


class BinFilesMissingException(Exception):
    pass


class ErrorException(Exception):
    pass


def log_debug(s):
    if VERBOSE:
        print("[DEBUG]\t%s" % s)


class Track:
    globalBlocksize = None

    def __init__(self, num, track_type):
        self.num = num
        self.indexes = []
        self.track_type = track_type
        self.sectors = None
        self.file_offset = None

        # All possible blocksize types. You cannot mix types on a disc, so we will use the first one we see and lock it in.
        #
        # AUDIO – Audio/Music (2352)
        # CDG – Karaoke CD+G (2448)
        # MODE1/2048 – CDROM Mode1 Data (cooked)
        # MODE1/2352 – CDROM Mode1 Data (raw)
        # MODE2/2336 – CDROM-XA Mode2 Data
        # MODE2/2352 – CDROM-XA Mode2 Data
        # CDI/2336 – CDI Mode2 Data
        # CDI/2352 – CDI Mode2 Data
        if not Track.globalBlocksize:
            if track_type in ['AUDIO', 'MODE1/2352', 'MODE2/2352', 'CDI/2352']:
                Track.globalBlocksize = 2352
            elif track_type == 'CDG':
                Track.globalBlocksize = 2448
            elif track_type == 'MODE1/2048':
                Track.globalBlocksize = 2048
            elif track_type in ['MODE2/2336', 'CDI/2336']:
                Track.globalBlocksize = 2336
            log_debug("Locked blocksize to %d" % Track.globalBlocksize)


class File:
    def __init__(self, filename):
        self.filename = filename
        self.tracks = []
        self.size = os.path.getsize(filename)


def read_cue_file(cue_path):
    files = []
    this_track = None
    this_file = None
    bin_files_missing = False

    f = open(cue_path, 'r')
    for line in f:
        m = re.search('FILE "?(.*?)"? BINARY', line)
        if m:
            this_path = os.path.join(os.path.dirname(cue_path), m.group(1))
            if not (os.path.isfile(this_path) or os.access(this_path, os.R_OK)):
                raise ErrorException(_("Bin file not found or not readable: %s") % this_path)
            else:
                this_file = File(this_path)
                files.append(this_file)
            continue

        m = re.search('TRACK (\d+) ([^\s]*)', line)
        if m and this_file:
            this_track = Track(int(m.group(1)), m.group(2))
            this_file.tracks.append(this_track)
            continue

        m = re.search('INDEX (\d+) (\d+:\d+:\d+)', line)
        if m and this_track:
            this_track.indexes.append(
                {'id': int(m.group(1)), 'stamp': m.group(2), 'file_offset': cuestamp_to_sectors(m.group(2))})
            continue

    if len(files) == 1:
        # only 1 file, assume splitting, calc sectors of each
        next_item_offset = files[0].size // Track.globalBlocksize
        for t in reversed(files[0].tracks):
            t.sectors = next_item_offset - t.indexes[0]["file_offset"]
            next_item_offset = t.indexes[0]["file_offset"]

    for f in files:
        log_debug("-- File --")
        log_debug("Filename: %s" % f.filename)
        log_debug("Size: %d" % f.size)
        log_debug("Tracks:")

        for t in f.tracks:
            log_debug("  -- Track --")
            log_debug("  Num: %d" % t.num)
            log_debug("  Type: %s" % t.track_type)
            if t.sectors: log_debug("  Sectors: %s" % t.sectors)
            log_debug("  Indexes: %s" % repr(t.indexes))

    return files


def sectors_to_cuestamp(sectors):
    # 75 sectors per second
    minutes = sectors / 4500
    fields = sectors % 4500
    seconds = fields / 75
    fields = sectors % 75
    return '%02d:%02d:%02d' % (minutes, seconds, fields)


def cuestamp_to_sectors(stamp):
    # 75 sectors per second
    m = re.match("(\d+):(\d+):(\d+)", stamp)
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    fields = int(m.group(3))
    return fields + (seconds * 75) + (minutes * 60 * 75)


# Generates track filename based on redump naming convention
# (Note: prefix may contain a fully qualified path)
def track_filename(prefix, track_num, track_count):
    # Redump is strangely inconsistent in their datfiles and cuesheets when it
    # comes to track numbers. The naming convention currently seems to be:
    # If there are less than 10 tracks: "Track 1", "Track 2", etc.
    # If there are more than 10 tracks: "Track 01", "Track 02", etc.
    #
    # It'd be nice if it were consistently %02d!
    #
    if track_count > 9:
        return "%s (Track %02d).bin" % (prefix, track_num)
    return "%s (Track %d).bin" % (prefix, track_num)


# Generates a 'merged' cuesheet, that is, one bin file with tracks indexed within.
def gen_merged_cuesheet(basename, files):
    cuesheet = 'FILE "%s.bin" BINARY\n' % basename
    # One sector is (BLOCKSIZE) bytes
    sector_pos = 0
    for f in files:
        for t in f.tracks:
            cuesheet += '  TRACK %02d %s\n' % (t.num, t.track_type)
            for i in t.indexes:
                cuesheet += '    INDEX %02d %s\n' % (i['id'], sectors_to_cuestamp(sector_pos + i['file_offset']))
        sector_pos += f.size / Track.globalBlocksize
    return cuesheet


# Generates a 'split' cuesheet, that is, with one bin file for every track.
def gen_split_cuesheet(basename, merged_file):
    cuesheet = ""
    for t in merged_file.tracks:
        track_fn = track_filename(basename, t.num, len(merged_file.tracks))
        cuesheet += 'FILE "%s" BINARY\n' % track_fn
        cuesheet += '  TRACK %02d %s\n' % (t.num, t.track_type)
        for i in t.indexes:
            sector_pos = i['file_offset'] - t.indexes[0]['file_offset']
            cuesheet += '    INDEX %02d %s\n' % (i['id'], sectors_to_cuestamp(sector_pos))
    return cuesheet


# Merges files together to new file `merged_filename`, in listed order.
def merge_files(merged_filename, files):
    if os.path.exists(merged_filename):
        raise ErrorException(_('Target bin path already exists: %s') % merged_filename)

    # cat is actually a bit faster, but this is multi-platform and no special-casing
    chunksize = 1024 * 1024
    with open(merged_filename, 'wb') as outfile:
        for f in files:
            with open(f.filename, 'rb') as infile:
                while True:
                    chunk = infile.read(chunksize)
                    if not chunk:
                        break
                    outfile.write(chunk)
    return True


# Writes each track in a File to a new file
def split_files(new_basename, merged_file):
    with open(merged_file.filename, 'rb') as infile:
        # Check all tracks for potential file-clobbering first before writing anything
        for t in merged_file.tracks:
            out_name = track_filename(new_basename, t.num, len(merged_file.tracks))
            if os.path.exists(out_name):
                raise ErrorException(_('Target bin path already exists: %s') % out_name)

        for t in merged_file.tracks:
            chunksize = 1024 * 1024
            out_name = track_filename(new_basename, t.num, len(merged_file.tracks))
            tracksize = t.sectors * Track.globalBlocksize
            written = 0
            with open(out_name, 'wb') as outfile:
                while True:
                    if chunksize + written > tracksize:
                        chunksize = tracksize - written
                    chunk = infile.read(chunksize)
                    outfile.write(chunk)
                    written += chunksize
                    if written == tracksize:
                        break
    return True


class LwtbinmergeguiApp:
    icon_data = b'''\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x000\x00\x00\x000\x08\x00\x00\x00\x00ri\xa6[\x00\x00\x02\xb8IDATH\xc7\xcd\x95[HTQ\x14\x86\xbf\xadS$A\x11\xa3]\xa0\x8c\xe8B(\xa2\x18biETFL\x84\x06\x19\xfa QA\x81/Y\x91Rx\x81\x88\xa4B\xd2\x82\x8a\xa6\xb2(\xa3\x8b\x0f\x8a(1\x16j\x92d\xa5y)\xcdr*\x13,G\'\xd4J\xcdq\\=\x8c\x97I\x8f\xa2\x86\xe0z\xd8\xe7\xc0^\xdf^\xfb\xdf\xeb?\xfb\xc0\xb4\x0b\x05<\xf2\x1fgry\xb8\x030[+\x07i\x19#\xdfW\xbf\xdc\xf1b6\x8e\xaf\x80\xd1\x0c\xb8Ld\xff2Q@M\x14`\x00P\x00lm\x89\x07 \xbf\xab\xab\x00 \xb4=\x04h\xeb\xea\xa8\x08\x1a\x0e8\xc2\xdd}\t\x80ik]\xed\xa6"\xe0\xc8\x9c8\x981\xb7\xb9\xd6\xf7\xb96\xd0\xe78\xd3\x90&?\xff\xc6\x8d\xc0\xfa_\x1b\xc0\x86)\xf0474\x01\xe9o\xc2W\xa8\x03\x0e\xb9\xa6\xcf\x8c\x02\x84\x04\x96\x8d\t\xe8\xc0\x158\x80\x8d\xe8\xfe\xa9nM\xa0?~\xfa\xc2\x9a?\x10@\x0ck\x01x\x8c\xe9\xdf\x14\xf3u\x00\xc2EDnqN\xa4W\xd28#a$J\x12b\xef\x912\xa7N\xeb\x86\xb8\x82\\\x11\x8a\x89m\x88P\x99\xa9\xb4fdq\xca\xa7\r\xd3\xec\xee\xa7\xc9hV\x98\x02/\xf1\x9f@z\x0e<0\x10\x97GL\xc5\xbb\x0b\x90\xf3\xa6\xfc\x06\xe4UVe\xcd\xd3\xd6P( E|\xb4\xc6Kg\x8bd"\xbd\x16)\xa5O\x9aD\x02\x8748\x01\xc7\xe4\xecM\xe9\xa4\xd7\xd4d\x83\x06ALX\xda\xb1\x97\x11!%\x9a\xa2S\x08\xda\xd2\xe8\x16\xe5\x9a\xb1\xe8\x03\x94\x02\x0bS=jA\xb8\xffg\x85\xb6\xe8/\xde\x9e\x97H\xe06v\xb0\x01>\x87[\xd6\x0e~iZ\xc0\xaby\x9c\xaf^\xf9\x89\xd6\x15\xe0\x03\xe4gyD\x82"`V\xbd6p\x8d\x1ar(!\xd3\xad\xb1\xda\xa7\x18\\v\xd9/\xa3\xbc+^rt\x94N\xbf?\x01_\x02\xc0\xf8\xdd\x9a\x07o/\x92X\xbf\xfbM\x8b\xa54\x08\xcdS\x9a.\xd6p\xb2w\xcc2\xbbH\xcf\xc9\xb0`\xc5\xb7\x14\x88\xf0J\x84\xd5\xfbc\x81\xe3\xbf/k\x896\x8b\x88\x08\x05"\xd2\x0c4\x08\x90*\xb9\x10*\xf5\x9a\x1a\x96\xab*\x94BPj\x01\xe0\xc9U\xb0c\xf0 \xcd\xb9s\xba\xe1w\'\xc2=\xf5\xec\nw\xf9\xbc\x03\x04{\xe1\xf5\xa5]2\x8ah\xe5\x18"#\x0c\xb0\xbd\xe6\xc9b\x10\xeex%\xbd\xd09-\xa8\x1b\x018\x1e~z\xbd\x17\x0f\xf7(\xf6\x19\xe6\xaf\xebQ\xa3lI9jf\xd3|0\x99\xf3Do\xc3\x05\x0e\x06\xa3F\x03\xac?\x80\x1f\xb2\x93FV\xbd\x8eE\xbfw~G\x1f\xd9\xd9XZ\'yk\xd4\x0f\x88\x96)\xb2\x86L\xdeK\xbe\xc6a\xcb\x8c\xfc\xb5!\xe0\xa7\xfa\x81r\xff\xcdc\xa4;\x81eL\xcb\xf8\x0b\x1d\x03\x1e\x05\xcc\x11\xaa^\x00\x00\x00\x00IEND\xaeB`\x82'''

    def __init__(self, master=None, translator=None):
        import platform
        self.themes = {'Windows': 'vista', 'Linux': 'clam', 'Darwin': 'aqua'}
        self.root_level = tk.Tk() if master is None else tk.Toplevel(master)
        icon_pic = tk.PhotoImage(data=LwtbinmergeguiApp.icon_data)
        self.root_level.iconphoto(False, icon_pic)

        ttk.Style().theme_use(
            self.themes.get(platform.system(), 'alt')
        )

        self.root_level.title(_("Binmerge GUI by load word team"))
        self.root_level.geometry('900x350')
        notebook = ttk.Notebook(self.root_level, width=250)

        self.merge_frame = ttk.Frame(notebook)
        self.merge_cue_frame = ttk.Labelframe(self.merge_frame)
        self.merge_cue_frame.configure(height=150, text=_('Source .CUE file:'))
        self.merge_input_cue = ttk.Entry(self.merge_cue_frame)
        self.merge_input_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.merge_source_cue_btn = ttk.Button(self.merge_cue_frame)
        self.merge_source_cue_btn.configure(text=_('Browse source .CUE'))
        self.merge_source_cue_btn.pack(expand=True, fill="x", side="right", padx=5, pady=5)
        self.merge_source_cue_btn.configure(command=self.merge_source_cue_action)
        self.merge_cue_frame.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.merge_save_cue = ttk.Labelframe(self.merge_frame)
        self.merge_save_cue.configure(height=200, text=_('Destination CUE/BIN:'))
        self.merge_output_cue = ttk.Entry(self.merge_save_cue)
        self.merge_output_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.merge_filename_to_save = None
        self.merge_save_cue_btn = ttk.Button(self.merge_save_cue)
        self.merge_save_cue_btn.configure(text=_('Set merged .CUE'))
        self.merge_save_cue_btn.pack(expand=True, fill="x", side="top", padx=5, pady=5)
        self.merge_save_cue_btn.configure(command=self.merge_save_cue_action)
        self.merge_save_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")

        self.merge_btn = ttk.Button(self.merge_frame)
        self.merge_btn.configure(text=_('Merge Tracks!'))
        self.merge_btn.pack(expand=True, fill="both", side="top", padx=5, pady=5)
        self.merge_btn.configure(command=self.merge_btn_action)

        self.frame_split = ttk.Frame(notebook)
        self.split_cue_frame = ttk.Labelframe(self.frame_split)
        self.split_cue_frame.configure(height=150, text=_('Source .CUE file:'))
        self.split_input_cue = ttk.Entry(self.split_cue_frame)
        self.split_input_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.split_source_cue_btn = ttk.Button(self.split_cue_frame)
        self.split_source_cue_btn.configure(text=_('Browse source .CUE'))
        self.split_source_cue_btn.pack(expand=True, fill="x", side="right", padx=5, pady=5)
        self.split_source_cue_btn.configure(command=self.split_source_cue_action)
        self.split_cue_frame.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.split_save_cue = ttk.Labelframe(self.frame_split)
        self.split_save_cue.configure(height=200, text=_('Destination files:'))
        self.split_output_cue = ttk.Entry(self.split_save_cue)
        self.split_output_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")
        self.split_output_cue_to_save = None
        self.split_save_cue_btn = ttk.Button(self.split_save_cue)
        self.split_save_cue_btn.configure(text=_('Set split .CUE'))
        self.split_save_cue_btn.pack(expand=True, fill="x", side="top", padx=5, pady=5)
        self.split_save_cue_btn.configure(command=self.split_output_cue_destination_action)
        self.split_save_cue.pack(side="top", padx=5, pady=5, expand=True, fill="x")

        self.split_btn = ttk.Button(self.frame_split)
        self.split_btn.configure(text=_('Split Tracks!'))
        self.split_btn.pack(expand=True, fill="both", side="top", padx=5, pady=5)
        self.split_btn.configure(command=self.split_btn_action)

        self.log_frame = tk.Frame(self.root_level)
        self.log_label = ttk.Labelframe(self.log_frame)
        self.log_label.configure(text=_('Log:'))
        self.log_txt = tkscrolled.ScrolledText(self.log_label, wrap=tk.WORD)
        self.log_txt.configure(height=10, width=50)
        self.log_txt.pack(anchor="nw", expand=True, fill="both", side="top", padx=5, pady=5)
        self.log_label.pack(anchor="nw", expand=True, fill="both", side="right", padx=5, pady=5)
        self.log_frame.pack(anchor="nw", expand=True, fill="both", side="right", padx=5, pady=5)

        self.log_txt.insert(
            tk.END,
            _("Binmerge-GUI v1.0 by load word team, based on Chris Putnam's binmerge.\n"
              "This is free software released under the GPL 3 License.\n"
              "Visit our website for more information http://loadwordteam.com\n\n"))
        # Main widget

        notebook.add(self.merge_frame, text=_('Merge Tracks'))
        notebook.add(self.frame_split, text=_('Split Tracks'))

        notebook.pack(pady=5, expand=True, fill='both')
        
        try:
            import pyi_splash
            pyi_splash.close()
        except:
            pass


        self.main_window = self.root_level

    def disable_merge_ui(self):
        self.root_level.config(cursor="clock")
        self.merge_btn.configure(text=_('Please wait'), state=tk.DISABLED)
        self.merge_save_cue_btn.configure(state=tk.DISABLED)
        self.merge_source_cue_btn.configure(state=tk.DISABLED)
        self.merge_input_cue.configure(state=tk.DISABLED)
        self.merge_output_cue.configure(state=tk.DISABLED)
        self.root_level.update()

    def disable_split_ui(self):
        self.root_level.config(cursor="clock")
        self.split_btn.configure(text=_('Please wait'), state=tk.DISABLED)
        self.split_save_cue_btn.configure(state=tk.DISABLED)
        self.split_source_cue_btn.configure(state=tk.DISABLED)
        self.split_input_cue.configure(state=tk.DISABLED)
        self.split_output_cue.configure(state=tk.DISABLED)
        self.root_level.update()

    def enable_merge_ui(self):
        self.root_level.config(cursor="arrow")
        self.merge_btn.configure(text=_('Merge Tracks!'), state=tk.NORMAL)
        self.merge_save_cue_btn.configure(state=tk.NORMAL)
        self.merge_source_cue_btn.configure(state=tk.NORMAL)
        self.merge_input_cue.configure(state=tk.NORMAL)
        self.merge_output_cue.configure(state=tk.NORMAL)
        self.root_level.update()

    def enable_split_ui(self):
        self.root_level.config(cursor="arrow")
        self.split_btn.configure(text=_('Split Tracks!'), state=tk.NORMAL)
        self.split_save_cue_btn.configure(state=tk.NORMAL)
        self.split_source_cue_btn.configure(state=tk.NORMAL)
        self.split_input_cue.configure(state=tk.NORMAL)
        self.split_output_cue.configure(state=tk.NORMAL)
        self.root_level.update()

    def log(self, message):
        when = datetime.datetime.now()
        self.log_txt.insert(tk.END, f"[{when.strftime('%H:%M:%S')}] {message}\n")
        self.log_txt.see(tk.END)

    def run(self):
        self.main_window.mainloop()

    def split_source_cue_action(self):
        filename = filedialog.askopenfilename(
            title=_("Choose a .cue file to split"),
            filetypes=[(_('cue files'), ('.cue', '.CUE')), (_('all files'), '.*')]
        )
        if filename and filename.strip():
            self.split_input_cue.delete(0, tk.END)
            self.split_input_cue.insert(0, filename)
            self.log(_("CUE source: %s") % filename)

    def split_output_cue_destination_action(self):

        self.split_output_cue_to_save = filedialog.SaveAs(
            initialfile=_('split_image.cue'),
            title=_("Save a split image with a .cue file"),
            filetypes=[(_('cue files'), ('.cue', '.CUE')), (_('all files'), '.*')]
        ).show()

        if self.split_output_cue_to_save:
            self.split_output_cue.delete(0, tk.END)
            self.split_output_cue.insert(0, self.split_output_cue_to_save)
            self.log(_("CUE destination: %s") % self.split_output_cue_to_save)

    def split_btn_action(self):
        import pathlib

        cuefile = self.split_input_cue.get()

        if not cuefile.strip() and self.split_output_cue_to_save:
            messagebox.showerror(_("Can't split!"), _("Make sure you selected the source cue file!"))
            return
        elif cuefile.strip() and not self.split_output_cue_to_save:
            messagebox.showerror(_("Can't split!"), _("Make sure you set the final cue file!"))
            return
        elif not cuefile.strip() and not self.split_output_cue_to_save:
            messagebox.showerror(_("Can't merge!"), _("You have to select source and split cue file!"))
            return

        self.log_info(_("Opening cue: %s") % cuefile)
        try:
            self.disable_split_ui()
            try:
                cue_map = read_cue_file(cuefile)
            except Exception:
                self.log_error(_("Error parsing cuesheet. Is it valid?"))
                self.enable_split_ui()
                return

            output_cue_path = pathlib.Path(self.split_output_cue_to_save)
            cuesheet = gen_split_cuesheet(output_cue_path.stem, cue_map[0])
            self.log(_("Splitting started, it will take a while, don't panic!"))
            if split_files(output_cue_path.parent.resolve() / output_cue_path.stem, cue_map[0]):
                self.log(_("Wrote %d bin files") % len(cue_map[0].tracks))
            else:
                self.log_error(_("Unable to split bin files."))
                self.enable_split_ui()
                return False

            with open(output_cue_path.resolve(), 'w', newline='\r\n') as f:
                f.write(cuesheet)
            self.log_info(_("Wrote new cue: %s") % output_cue_path.resolve())

        except ErrorException as exc:
            self.log_error(str(exc))
        except Exception:
            self.log_error(_("Error parsing cuesheet. Is it valid?"))

        self.enable_split_ui()

    def merge_source_cue_action(self):
        filename = filedialog.askopenfilename(
            title=_("Choose a .cue file to merge"),
            filetypes=[(_('cue files'), ('.cue', '.CUE')), (_('all files'), '.*')]
        )
        if filename and filename.strip():
            self.merge_input_cue.delete(0, tk.END)
            self.merge_input_cue.insert(0, filename)
            self.log(_("CUE source: %s") % filename)

    def merge_save_cue_action(self):

        self.merge_filename_to_save = filedialog.SaveAs(
            initialfile=_('merged_image.cue'),
            title=_("Save a merged .cue file"),
            filetypes=[(_('cue files'), ('.cue', '.CUE')), (_('all files'), '.*')]
        ).show()

        if self.merge_filename_to_save:
            self.merge_output_cue.delete(0, tk.END)
            self.merge_output_cue.insert(0, self.merge_filename_to_save)
            self.log(_("CUE destination: %s") % self.merge_filename_to_save)

    def log_info(self, msg):
        self.log(msg)

    def log_error(self, msg):
        self.log(_("ERROR %s") % msg)

    def merge_btn_action(self):
        import pathlib

        cuefile = self.merge_input_cue.get()

        if not cuefile.strip() and self.merge_filename_to_save:
            messagebox.showerror(_("Can't merge!"), _("Make sure you selected the source cue file!"))
            return
        elif cuefile.strip() and not self.merge_filename_to_save:
            messagebox.showerror(_("Can't merge!"), _("Make sure you set the destination cue file!"))
            return
        elif not cuefile.strip() and not self.merge_filename_to_save:
            messagebox.showerror(_("Can't merge!"), _("You have to select source and merged cue file!"))
            return

        self.log_info(_("Opening cue: %s") % cuefile)
        try:
            self.disable_merge_ui()
            try:
                cue_map = read_cue_file(cuefile)
            except Exception:
                self.log_error(_("Error parsing cuesheet. Is it valid?"))
                self.enable_merge_ui()
                return
            cue_out = pathlib.Path(self.merge_filename_to_save)
            self.log_info(_("Merge operation started, it will take a while, don't panic!"))
            self.merge_btn['state'] = tk.DISABLED

            cuesheet = gen_merged_cuesheet(cue_out.stem, cue_map)

            self.log_info(_("Merging %d tracks...") % len(cue_map))

            if merge_files(cue_out.with_suffix('.bin').resolve(), cue_map):
                self.log_info(_("Wrote a new bin: %s") % cue_out.with_suffix('.bin').resolve())
            else:
                self.log_error(_("Unable to merge bin files."))
                self.enable_merge_ui()
                return False

            with open(self.merge_filename_to_save, 'w', newline='\r\n') as f:
                f.write(cuesheet)
            self.log_info(_("Wrote new cue: %s") % self.merge_filename_to_save)

        except ErrorException as exc:
            self.log_error(str(exc))

        self.enable_merge_ui()


if __name__ == "__main__":
    app = LwtbinmergeguiApp()
    app.run()
