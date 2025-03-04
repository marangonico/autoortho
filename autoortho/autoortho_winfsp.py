"""A memory file system implemented on top of winfspy.

Useful for testing and as a reference.
"""

import os
import re
import sys
import time
import argparse
import threading
import traceback
from functools import wraps, lru_cache, cache, cached_property
from pathlib import Path, PureWindowsPath

from winfspy import (
    FileSystem,
    BaseFileSystemOperations,
    enable_debug_log,
    FILE_ATTRIBUTE,
    CREATE_FILE_CREATE_OPTIONS,
    NTStatusObjectNameNotFound,
    NTStatusDirectoryNotEmpty,
    NTStatusNotADirectory,
    NTStatusObjectNameCollision,
    NTStatusAccessDenied,
    NTStatusEndOfFile,
    NTStatusMediaWriteProtected,
)
from winfspy.plumbing.win32_filetime import filetime_now
from winfspy.plumbing.security_descriptor import SecurityDescriptor

import getortho
from aoconfig import CFG
from winfsp_shim import OperationsShim
import flighttrack

import logging
log = logging.getLogger(__name__)


def operation(fn):
    """Decorator for file system operations.

    Provides both logging and thread-safety
    """
    name = fn.__name__

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        head = args[0] if args else None
        tail = args[1:] if args else ()
        try:
            if self.biglock:
                with self._thread_lock:
                    result = fn(self, *args, **kwargs)
            else:
                result = fn(self, *args, **kwargs)
        except Exception as exc:
            traceback.print_exc()
            logging.info(f" NOK | {name:20} | {head!r:20} | {tail!r:20} | {exc!r}")
            raise
        else:
            logging.info(f" OK! | {name:20} | {head!r:20} | {tail!r:20} | {result!r}")
            return result

    return wrapper


class BaseFileObj:
    
    @property
    def name(self):
        """File name, without the path"""
        return self.path.name

    @property
    def file_name(self):
        """File name, including the path"""
        return str(self.path)

    def __init__(self, path, attributes, security_descriptor):
        self.path = path
        self.attributes = attributes
        self.security_descriptor = security_descriptor
        now = filetime_now()
        self.creation_time = now
        self.last_access_time = now
        self.last_write_time = now
        self.change_time = now
        self.index_number = 0
        self.file_size = 0
        self.allocation_size = 0

    def get_file_info(self):
        return {
            "file_attributes": self.attributes,
            "allocation_size": self.allocation_size,
            "file_size": self.file_size,
            "creation_time": self.creation_time,
            "last_access_time": self.last_access_time,
            "last_write_time": self.last_write_time,
            "change_time": self.change_time,
            "index_number": self.index_number,
        }

    def __repr__(self):
        return f"{type(self).__name__}:{self.file_name}"


class FileObj(BaseFileObj):

    allocation_unit = 4096
    #tile = None
    tile_id = None

    def __init__(self, path, attributes, security_descriptor, allocation_size=0):
        super().__init__(path, attributes, security_descriptor)
        #self.data = bytearray(allocation_size)
        self.attributes |= FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
        assert not self.attributes & FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY


class FolderObj(BaseFileObj):
    def __init__(self, path, attributes, security_descriptor):
        super().__init__(path, attributes, security_descriptor)
        self.allocation_size = 0
        assert self.attributes & FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY


class OpenedObj:
    def __init__(self, file_obj, handle):
        self.file_obj = file_obj
        self.handle = handle

    def __repr__(self):
        return f"{type(self).__name__}:{self.file_obj.file_name}"


class AutoorthoOperations(OperationsShim):
    def __init__(self, root, volume_label, tile_cache, read_only=False):
        super().__init__()
        if len(volume_label) > 31:
            raise ValueError("`volume_label` must be 31 characters long max")

        max_file_nodes = 1024
        max_file_size = 16 * 1024 * 1024
        file_nodes = 1

        self._volume_info = {
            "total_size": max_file_nodes * max_file_size,
            "free_size": (max_file_nodes - file_nodes) * max_file_size,
            "volume_label": volume_label,
        }

        self.read_only = read_only
        self.root = root
        self._root_path = PureWindowsPath("/")
        self._root_obj = FolderObj(
            self._root_path,
            FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY,
            SecurityDescriptor.from_string("O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;WD)"),
            #SecurityDescriptor.from_string("O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"),
        )
        self._entries = {self._root_path: self._root_obj}
        self._thread_lock = threading.RLock()
        self.dds_re = re.compile(".*\\\(\d+)[-_](\d+)[-_]((?!ZL)\S*)(\d{2}).dds")
        self.tc = tile_cache

        # Do lots of locking
        #self.biglock = True
        self.biglock = False

        self.startup = True

    # Winfsp operations

    def get_volume_info(self):
        return self._volume_info

    #@operation
    def set_volume_label(self, volume_label):
        self._volume_info["volume_label"] = volume_label

    #@operation
    def get_security_by_name(self, file_name):
        file_name = PureWindowsPath(file_name)
        #print(f"GET_SECURITY_BY_NAME: {file_name}")

        full_path = self._full_path(str(file_name))
       
        if os.path.isdir(full_path):
            file_obj = self.add_obj(file_name, self._root_path, True)
        else:
            file_obj = self.add_obj(file_name, self._root_path, False)

        return (
            file_obj.attributes,
            file_obj.security_descriptor.handle,
            file_obj.security_descriptor.size,
        )

    #@operation
    @lru_cache(maxsize=4096)
    def add_obj(self, file_name, parent, isdir):
        file_name = PureWindowsPath(file_name)

        # `granted_access` is already handle by winfsp
        # `allocation_size` useless for us

        if isdir:
            file_attributes = FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            security_descriptor = self._root_obj.security_descriptor
            file_obj = FolderObj(
                file_name, file_attributes, security_descriptor
            )
        else:
            file_attributes = FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
            security_descriptor = self._root_obj.security_descriptor
            file_obj = FileObj(
                file_name, file_attributes, security_descriptor
            )

        return file_obj
        

    #@operation
    #@lru_cache
    @cache
    def get_security(self, file_context):
        return file_context.file_obj.security_descriptor

    #@operation
    def open(self, file_name, create_options, granted_access):
        #print(f"OPEN: {file_name} {create_options} {granted_access}")
        file_name = PureWindowsPath(file_name)
        # Retrieve file

        path = str(file_name)
        
        h = -1
        m = self.dds_re.match(path)
        if m:
            if self.startup:
                # First matched file
                log.info("First matched DDS file detected.  Start flight tracker.")
                flighttrack.ft.start()
                self.startup = False

            #print(f"MATCH! {path}")
            row, col, maptype, zoom = m.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            #print(f"OPEN: DDS file {path}, offset {offset}, length {length} (%s) " % str(m.groups()))
            file_obj = self.add_obj(path, self._root_path, False)
            tile = self.tc._open_tile(row, col, maptype, zoom) 
            file_obj.tile_id = (row, col, maptype, zoom)
        else:
            full_path = self._full_path(path)
            if os.path.isdir(full_path):
                file_obj = self.add_obj(path, self._root_path, True)
            elif os.path.exists(full_path):
                file_obj = self.add_obj(path, self._root_path, False)
                h = open(full_path, "rb")
            else:
                raise NTStatusObjectNameNotFound()
        return OpenedObj(file_obj, h)

    #@operation
    def close(self, file_context):
        #print(f"CLOSE: {file_context}")
        path = str(file_context.file_obj.path)
        m = self.dds_re.match(path)
        if m:
            self.tc._close_tile(*file_context.file_obj.tile_id)
        else:
            fh = file_context.handle
            if fh != -1:
                #print(f"Closing {file_context}.  FH: {fh}")
                #os.close(fh)
                fh.close()
                file_context.handle = None

    #@operation
    #@lru_cache(maxsize=4096)
    @cache
    def get_file_info(self, file_context):
        #print(f"GET_FILE_INFO: {file_context}")
        path = str(file_context.file_obj.path)
        #{'file_attributes': 32, 'allocation_size': 0, 'file_size': 0, 'creation_time': 133091265506258958, 'last_access_time': 133091265506258958, 'last_write_time': 133091265506258958, 'change_time': 133091265506258958, 'index_number': 0}
        #os.stat_result(st_mode=33206, st_ino=562949953931301, st_dev=1421433975, st_nlink=1, st_uid=0, st_gid=0, st_size=832, st_atime=1664639212, st_mtime=1653275838, st_ctime=1653275838)

        m = self.dds_re.match(path)
        if m:
            #print(f"MATCH: Set file size")
            file_context.file_obj.file_size = 22369744
        else:
            full_path = self._full_path(path)
            if os.path.isfile(full_path):
                st = os.lstat(full_path)
                file_context.file_obj.file_size = st.st_size
        return file_context.file_obj.get_file_info()

    #@lru_cache
    @cache
    def listlocal(self, path):
        return os.listdir(path)

    #@operation
    @lru_cache
    def read_directory(self, file_context, marker, buffer_len):
        print(f"READ_DIRECTORY {file_context} {marker}")
        entries = []
        file_obj = file_context.file_obj

        # Not a directory
        if isinstance(file_obj, FileObj):
            raise NTStatusNotADirectory()

        path = str(file_obj.path)

        full_path = self._full_path(path)
        #print(f"READDIR: {path}")

        #entries = []
        dirents = ['.', '..']
        if os.path.isdir(full_path):
            #dirents.extend(os.listdir(full_path))
            dirents.extend(self.listlocal(full_path))

        if marker:
            #print(f"MARKER! {marker} {buffer_len}")
            marker_idx = dirents.index(marker)
            dirents = dirents[marker_idx + 1 : marker_idx + 1024]
            #print(f"DIRENTS LEN: {len(dirents)}")

        #for r in dirents[0:1024]:
        for r in dirents:
            #print(r)
            tpath = os.path.join(path, r)
            if os.path.isdir(os.path.join(full_path, r)):
                obj = self.add_obj(tpath, self._root_path, True)
                tobj = {"file_name": r, **obj.get_file_info()}
            else:
                st = os.lstat(os.path.join(full_path, r))
                obj = self.add_obj(tpath, self._root_path, False)
                obj.file_size = st.st_size
                tobj = {"file_name": r, **obj.get_file_info()}

            #print(tobj)
            #entries.append(tobj)
            yield tobj

        return

        print(f"NUM ENTRIES: {len(entries)}")
        entries = sorted(entries, key=lambda x: x["file_name"])
        for e in entries:
            yield e
        return

        return entries

        # No filtering to apply
        if marker is None:
            print(f"READ_DIR: {entries}")
            return entries

        # Filter out all results before the marker
        for i, entry in enumerate(entries):
            if entry["file_name"] == marker:
                print("READ_DIR RETURNING ....")
                #print(entries[i + 1 :])
                return entries[i + 1 :]
        #return
    

    #@operation
    @lru_cache
    def get_dir_info_by_name(self, file_context, file_name):
        #print(f"GET DIR INFO BY NAME: {file_context} {file_name}")
        tpath = os.path.join(file_context.file_obj.path, file_name)
        obj = self.add_obj(tpath, self._root_path, True)

    def _full_path(self, partial):
        #print(f"_FULL_PATH: {partial}")
        if partial.startswith("\\"):
            partial = partial[1:]
        path = os.path.abspath(os.path.join(self.root, partial))
        return path

    #@operation
    def read(self, file_context, offset, length):
        #print(f"READ: P:{file_context.file_obj.path} O:{offset} L:{length}")
        data = None

        path = str(file_context.file_obj.path)
        m = self.dds_re.match(path)
        if m:
            #print(f"READ MATCH: {path}")
            #data = file_context.file_obj.tile.read_dds_bytes(offset, length)
            #row, col, maptype, zoom = file_context.file_obj.tile_id.split("_")
            row, col, maptype, zoom = file_context.file_obj.tile_id
            tile = self.tc._get_tile(row, col, maptype, zoom)
            data = tile.read_dds_bytes(offset, length)
            #print(f"READ: {len(data)} bytes")
        
        if not data:
            fh = file_context.handle
            fh.seek(offset)
            data = fh.read(length)
        #print(f"LEN DATA: {len(data)}")
        return data
        #return file_context.file_obj.read(offset, length)

    #@operation
    def cleanup(self, file_context, file_name, flags) -> None:
        #print(f"CLEANUP: {file_context} {file_name} {flags}")
        return

    #@operation
    def flush(self, file_context) -> None:
        pass

    def show_stats(self):
        method_list = [method for method in dir(AutoorthoOperations) if
                method.startswith('_') is False]

        self.tc.show_stats()
        for m_name in method_list:
            m = getattr(self, m_name)
            if hasattr(m, 'cache_info'):
                print(f"{m_name}: {m.cache_info()}")


def create_file_system(
    root, mountpoint, label="autoortho", prefix="", verbose=True, debug=False, testing=False
):
    if debug:
        enable_debug_log()

    if verbose:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    # The avast workaround is not necessary with drives
    # Also, it is not compatible with winfsp-tests
    root = Path(root)
    mountpoint = Path(mountpoint)
    is_drive = mountpoint.parent == mountpoint
    reject_irp_prior_to_transact0 = not is_drive and not testing
    tc = getortho.TileCacher(".cache") 
    operations = AutoorthoOperations(str(root), label, tc)
    fs = FileSystem(
        str(mountpoint),
        operations,
        sector_size=512,
        sectors_per_allocation_unit=1,
        volume_creation_time=filetime_now(),
        volume_serial_number=0,
        file_info_timeout=1000,
        case_sensitive_search=1,
        case_preserved_names=1,
        unicode_on_disk=1,
        persistent_acls=0,
        #post_cleanup_when_modified_only=1,
        um_file_context_is_user_context2=1,
        flush_and_purge_on_cleanup=1,
        file_system_name=str(mountpoint),
        prefix=prefix,
        debug=debug,
        reject_irp_prior_to_transact0=reject_irp_prior_to_transact0,
        # security_timeout_valid=1,
        # security_timeout=10000,
    )
    return fs


def main(root, mountpoint, label="autoorotho", prefix="", verbose=False, debug=False):
    #debug=True
    fs = create_file_system(root, mountpoint, label, prefix, verbose, debug)
    try:
        print("Starting FS")
        fs.start()
        print("FS started, keep it running forever")
        while True:
            result = input("AO> ").lower()
            if result == "s":
                fs.operations.show_stats()
            elif result == "q":
                break

    finally:
        print("Stopping FS")
        fs.stop()
        print("FS stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("mountpoint")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-l", "--label", type=str, default="memfs")
    parser.add_argument("-p", "--prefix", type=str, default="")
    args = parser.parse_args()
    main(args.root, args.mountpoint, args.label, args.prefix, args.verbose, args.debug)
