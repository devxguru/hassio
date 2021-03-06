"""Represent a snapshot file."""
import asyncio
import json
import logging
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory

import voluptuous as vol
from voluptuous.humanize import humanize_error

from .validate import SCHEMA_SNAPSHOT, ALL_FOLDERS
from .util import remove_folder
from ..const import (
    ATTR_SLUG, ATTR_NAME, ATTR_DATE, ATTR_ADDONS, ATTR_REPOSITORIES,
    ATTR_HOMEASSISTANT, ATTR_FOLDERS, ATTR_VERSION, ATTR_TYPE, ATTR_DEVICES,
    ATTR_IMAGE)
from ..tools import write_json_file

_LOGGER = logging.getLogger(__name__)


class Snapshot(object):
    """A signle hassio snapshot."""

    def __init__(self, config, loop, tar_file):
        """Initialize a snapshot."""
        self.loop = loop
        self.config = config
        self.tar_file = tar_file
        self._data = {}
        self._tmp = None

    @property
    def slug(self):
        """Return snapshot slug."""
        return self._data.get(ATTR_SLUG)

    @property
    def sys_type(self):
        """Return snapshot type."""
        return self._data.get(ATTR_TYPE)

    @property
    def name(self):
        """Return snapshot name."""
        return self._data[ATTR_NAME]

    @property
    def date(self):
        """Return snapshot date."""
        return self._data[ATTR_DATE]

    @property
    def addons(self):
        """Return snapshot date."""
        return self._data[ATTR_ADDONS]

    @property
    def folders(self):
        """Return list of saved folders."""
        return self._data[ATTR_FOLDERS]

    @property
    def repositories(self):
        """Return snapshot date."""
        return self._data[ATTR_REPOSITORIES]

    @repositories.setter
    def repositories(self, value):
        """Set snapshot date."""
        self._data[ATTR_REPOSITORIES] = value

    @property
    def homeassistant_version(self):
        """Return snapshot homeassistant version."""
        return self._data[ATTR_HOMEASSISTANT].get(ATTR_VERSION)

    @homeassistant_version.setter
    def homeassistant_version(self, value):
        """Set snapshot homeassistant version."""
        self._data[ATTR_HOMEASSISTANT][ATTR_VERSION] = value

    @property
    def homeassistant_devices(self):
        """Return snapshot homeassistant devices."""
        return self._data[ATTR_HOMEASSISTANT].get(ATTR_DEVICES)

    @homeassistant_devices.setter
    def homeassistant_devices(self, value):
        """Set snapshot homeassistant devices."""
        self._data[ATTR_HOMEASSISTANT][ATTR_DEVICES] = value

    @property
    def homeassistant_image(self):
        """Return snapshot homeassistant custom image."""
        return self._data[ATTR_HOMEASSISTANT].get(ATTR_IMAGE)

    @homeassistant_image.setter
    def homeassistant_image(self, value):
        """Set snapshot homeassistant custom image."""
        self._data[ATTR_HOMEASSISTANT][ATTR_IMAGE] = value

    @property
    def size(self):
        """Return snapshot size."""
        if not self.tar_file.is_file():
            return 0
        return self.tar_file.stat().st_size / 1048576  # calc mbyte

    def create(self, slug, name, date, sys_type):
        """Initialize a new snapshot."""
        # init metadata
        self._data[ATTR_SLUG] = slug
        self._data[ATTR_NAME] = name
        self._data[ATTR_DATE] = date
        self._data[ATTR_TYPE] = sys_type

        # init other constructs
        self._data[ATTR_HOMEASSISTANT] = {}
        self._data[ATTR_ADDONS] = []
        self._data[ATTR_REPOSITORIES] = []
        self._data[ATTR_FOLDERS] = []

    def snapshot_homeassistant(self, homeassistant):
        """Read all data from homeassistant object."""
        self.homeassistant_version = homeassistant.version
        self.homeassistant_devices = homeassistant.devices

        # custom image
        if homeassistant.is_custom_image:
            self.homeassistant_image = homeassistant.image

    def restore_homeassistant(self, homeassistant):
        """Write all data to homeassistant object."""
        homeassistant.devices = self.homeassistant_devices

        # custom image
        if self.homeassistant_image:
            homeassistant.set_custom(
                self.homeassistant_image, self.homeassistant_version)

    async def load(self):
        """Read snapshot.json from tar file."""
        if not self.tar_file.is_file():
            _LOGGER.error("No tarfile %s", self.tar_file)
            return False

        def _load_file():
            """Read snapshot.json."""
            with tarfile.open(self.tar_file, "r:") as snapshot:
                json_file = snapshot.extractfile("./snapshot.json")
                return json_file.read()

        # read snapshot.json
        try:
            raw = await self.loop.run_in_executor(None, _load_file)
        except (tarfile.TarError, KeyError) as err:
            _LOGGER.error(
                "Can't read snapshot tarfile %s -> %s", self.tar_file, err)
            return False

        # parse data
        try:
            raw_dict = json.loads(raw)
        except json.JSONDecodeError as err:
            _LOGGER.error("Can't read data for %s -> %s", self.tar_file, err)
            return False

        # validate
        try:
            self._data = SCHEMA_SNAPSHOT(raw_dict)
        except vol.Invalid as err:
            _LOGGER.error("Can't validate data for %s -> %s", self.tar_file,
                          humanize_error(raw_dict, err))
            return False

        return True

    async def __aenter__(self):
        """Async context to open a snapshot."""
        self._tmp = TemporaryDirectory(dir=str(self.config.path_tmp))

        # create a snapshot
        if not self.tar_file.is_file():
            return self

        # extract a exists snapshot
        def _extract_snapshot():
            """Extract a snapshot."""
            with tarfile.open(self.tar_file, "r:") as tar:
                tar.extractall(path=self._tmp.name)

        await self.loop.run_in_executor(None, _extract_snapshot)

    async def __aexit__(self, exception_type, exception_value, traceback):
        """Async context to close a snapshot."""
        # exists snapshot or exception on build
        if self.tar_file.is_file() or exception_type is not None:
            return self._tmp.cleanup()

        # validate data
        try:
            self._data = SCHEMA_SNAPSHOT(self._data)
        except vol.Invalid as err:
            _LOGGER.error("Invalid data for %s -> %s", self.tar_file,
                          humanize_error(self._data, err))
            raise ValueError("Invalid config") from None

        # new snapshot, build it
        def _create_snapshot():
            """Create a new snapshot."""
            with tarfile.open(self.tar_file, "w:") as tar:
                tar.add(self._tmp.name, arcname=".")

        if write_json_file(Path(self._tmp.name, "snapshot.json"), self._data):
            await self.loop.run_in_executor(None, _create_snapshot)
        else:
            _LOGGER.error("Can't write snapshot.json")

        self._tmp.cleanup()
        self._tmp = None

    async def import_addon(self, addon):
        """Add a addon into snapshot."""
        snapshot_file = Path(self._tmp.name, "{}.tar.gz".format(addon.slug))

        if not await addon.snapshot(snapshot_file):
            _LOGGER.error("Can't make snapshot from %s", addon.slug)
            return False

        # store to config
        self._data[ATTR_ADDONS].append({
            ATTR_SLUG: addon.slug,
            ATTR_NAME: addon.name,
            ATTR_VERSION: addon.version_installed,
        })

        return True

    async def export_addon(self, addon):
        """Restore a addon from snapshot."""
        snapshot_file = Path(self._tmp.name, "{}.tar.gz".format(addon.slug))

        if not await addon.restore(snapshot_file):
            _LOGGER.error("Can't restore snapshot for %s", addon.slug)
            return False

        return True

    async def store_folders(self, folder_list=None):
        """Backup hassio data into snapshot."""
        folder_list = folder_list or ALL_FOLDERS

        def _folder_save(name):
            """Intenal function to snapshot a folder."""
            slug_name = name.replace("/", "_")
            snapshot_tar = Path(self._tmp.name, "{}.tar.gz".format(slug_name))
            origin_dir = Path(self.config.path_hassio, name)

            try:
                with tarfile.open(snapshot_tar, "w:gz",
                                  compresslevel=1) as tar_file:
                    tar_file.add(origin_dir, arcname=".")

                self._data[ATTR_FOLDERS].append(name)
            except tarfile.TarError as err:
                _LOGGER.warning("Can't snapshot folder %s -> %s", name, err)

        # run tasks
        tasks = [self.loop.run_in_executor(None, _folder_save, folder)
                 for folder in folder_list]
        if tasks:
            await asyncio.wait(tasks, loop=self.loop)

    async def restore_folders(self, folder_list=None):
        """Backup hassio data into snapshot."""
        folder_list = folder_list or ALL_FOLDERS

        def _folder_restore(name):
            """Intenal function to restore a folder."""
            slug_name = name.replace("/", "_")
            snapshot_tar = Path(self._tmp.name, "{}.tar.gz".format(slug_name))
            origin_dir = Path(self.config.path_hassio, name)

            # clean old stuff
            if origin_dir.is_dir():
                remove_folder(origin_dir)

            try:
                with tarfile.open(snapshot_tar, "r:gz") as tar_file:
                    tar_file.extractall(path=origin_dir)
            except tarfile.TarError as err:
                _LOGGER.warning("Can't restore folder %s -> %s", name, err)

        # run tasks
        tasks = [self.loop.run_in_executor(None, _folder_restore, folder)
                 for folder in folder_list]
        if tasks:
            await asyncio.wait(tasks, loop=self.loop)
