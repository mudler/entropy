# -*- coding: utf-8 -*-
"""

    @author: Fabio Erculiani <lxnay@sabayon.org>
    @contact: lxnay@sabayon.org
    @copyright: Fabio Erculiani
    @license: GPL-2

    B{Entropy Package Manager Client Miscellaneous functions Interface}.

"""
import os
import bz2
import stat
import fcntl
import errno
import sys
import shutil
import time
import subprocess
import tempfile
import threading
import codecs
from datetime import datetime

from entropy.i18n import _
from entropy.const import etpConst, etpUi, const_debug_write, etpSys, \
    const_setup_file, initconfig_entropy_constants, const_pid_exists, \
    const_setup_perms, const_setup_entropy_pid, \
    const_isstring, const_convert_to_unicode, const_isnumber, \
    const_convert_to_rawstring
from entropy.exceptions import RepositoryError, SystemDatabaseError, \
    RepositoryPluginError, SecurityError, EntropyPackageException
from entropy.db.skel import EntropyRepositoryBase
from entropy.db.exceptions import Error as EntropyRepositoryError
from entropy.cache import EntropyCacher
from entropy.misc import FlockFile
from entropy.client.interfaces.db import ClientEntropyRepositoryPlugin, \
    InstalledPackagesRepository, AvailablePackagesRepository, GenericRepository
from entropy.client.mirrors import StatusInterface
from entropy.output import purple, bold, red, blue, darkgreen, darkred, brown, \
    teal

from entropy.db.exceptions import IntegrityError, OperationalError, \
    DatabaseError

import entropy.dep
import entropy.tools

class RepositoryMixin:

    def __get_repository_cache_key(self, repository_id):
        return (repository_id, etpConst['systemroot'],)

    def _validate_repositories(self, quiet = False):

        StatusInterface().clear()
        self._repo_error_messages_cache.clear()

        # clear live masking validation cache, if exists
        cl_id = self.sys_settings_client_plugin_id
        client_metadata = self._settings.get(cl_id, {})
        if "masking_validation" in client_metadata:
            client_metadata['masking_validation']['cache'].clear()

        def ensure_closed_repo(repoid):
            key = self.__get_repository_cache_key(repoid)
            for cache_obj in (self._repodb_cache, self._memory_db_instances):
                obj = cache_obj.pop(key, None)
                if obj is None:
                    continue
                try:
                    obj.close(_token = repoid)
                except OperationalError:
                    pass

        t2 = _("Please update your repositories now in order to remove this message!")

        del self._enabled_repos[:]
        _enabled_repos = []
        all_repos = self._settings['repositories']['order'][:]
        for repoid in self._settings['repositories']['order']:
            # open database
            try:
                dbc = self._open_repository(
                    repoid, _enabled_repos = all_repos)
                dbc.listConfigProtectEntries()
                dbc.validate()
                _enabled_repos.append(repoid)
            except RepositoryError as err:

                ensure_closed_repo(repoid)
                if quiet or etpUi['quiet']:
                    continue

                t = _("Repository") + " " + const_convert_to_unicode(repoid) \
                    + " " + _("is not available") + ". " + _("Cannot validate")
                self.output(
                    darkred(t),
                    importance = 1,
                    level = "warning"
                )
                self.output(
                    repr(err),
                    importance = 0,
                    level = "warning"
                )
                self.output(
                    purple(t2),
                    header = bold("!!! "),
                    importance = 1,
                    level = "warning"
                )
                continue # repo not available
            except (OperationalError, DatabaseError, SystemDatabaseError,) as err:

                ensure_closed_repo(repoid)
                if quiet:
                    continue

                t = _("Repository") + " " + repoid + " " + \
                    _("is corrupted") + ". " + _("Cannot validate")
                self.output(
                    darkred(t),
                    importance = 1,
                    level = "warning"
                )
                self.output(
                    repr(err),
                    importance = 0,
                    level = "warning"
                )
                continue

        # write back correct _enabled_repos
        self._enabled_repos.extend(_enabled_repos)

    def _init_generic_temp_repository(self, repoid, description,
        package_mirrors = None, temp_file = None):
        if package_mirrors is None:
            package_mirrors = []

        dbc = self.open_temp_repository(name = repoid, temp_file = temp_file)
        repo_key = self.__get_repository_cache_key(repoid)
        self._memory_db_instances[repo_key] = dbc

        # add to self._settings['repositories']['available']
        repodata = {
            'repoid': repoid,
            '__temporary__': True,
            'description': description,
            'packages': package_mirrors,
            'dbpath': temp_file,
        }
        added = self.add_repository(repodata)
        if not added:
            raise ValueError("repository not added, wtf?")
        return dbc

    def close_repositories(self, mask_clear = True):
        """
        Close all the previously opened (through open_repository()) repository
        instances. If mask_clear is True, package masking information will
        be cleared as well (by calling SystemSettings.clear()).

        @keyword mask_clear: clear package masking information if True
        @type mask_clear: bool
        """
        repo_cache = getattr(self, "_repodb_cache", {})
        for item, val in list(repo_cache.items()): # list() -> python3 support
            repository_id, root = item
            # in-memory repositories cannot be closed
            # otherwise everything will be lost, to
            # effectively close these repos you
            # must call remove_repository method
            if item in self._memory_db_instances:
                continue
            try:
                repo_cache.pop(item).close(_token = repository_id)
            except OperationalError as err: # wtf!
                sys.stderr.write("!!! Cannot close Entropy repos: %s\n" % (
                    err,))
        repo_cache.clear()

        # disable hooks during SystemSettings cleanup
        # otherwise it makes entropy.client.interfaces.repository crazy
        old_value = self._can_run_sys_set_hooks
        self._can_run_sys_set_hooks = False
        if mask_clear:
            self._settings.clear()
        self._can_run_sys_set_hooks = old_value

    def _open_repository(self, repository_id, _enabled_repos = None):
        # support for installed pkgs repository, got by issuing
        # repoid = etpConst['clientdbid']
        if repository_id == etpConst['clientdbid']:
            return self._installed_repository

        key = self.__get_repository_cache_key(repository_id)
        cached = self._repodb_cache.get(key)
        if cached is not None:
            return cached

        self._repodb_cache[key] = self._load_repository(repository_id,
            xcache = self.xcache, indexing = self._indexing,
            _enabled_repos = _enabled_repos)
        return self._repodb_cache[key]

    def open_repository(self, repository_id):
        """
        If you just want open a read-only repository, use this method.

        @param repository_id: repository identifier
        @type repository_id: string
        @return: EntropyRepositoryBase based instance
        @rtype: entropy.db.skel.EntropyRepositoryBase
        """
        return self._open_repository(repository_id)

    @staticmethod
    def get_repository(repoid):
        """
        Given a repository identifier, returns the repository class associated
        with it.
        NOTE: stub. When more EntropyRepositoryBase classes will be available,
        this method will start making more sense.
        WARNING: do not use this to open a repository. Please use
        Client.open_repository() instead.

        @param repoid: repository identifier
        @type repoid: string
        @return: EntropyRepositoryBase based class
        @rtype: class object
        """
        if repoid == etpConst['clientdbid']:
            return InstalledPackagesRepository
        return AvailablePackagesRepository

    def _is_package_repository(self, repository_id):
        """
        Determine whether given repository id is a package repository.

        @param repository_id: repository identifier
        @type repository_id: string
        @return: True, if package repository
        @rtype: bool
        """
        if not repository_id:
            return False
        return repository_id.endswith(etpConst['packagesext']) or \
            repository_id.endswith(etpConst['packagesext_webinstall'])

    def _load_repository(self, repoid, xcache = True, indexing = True,
        _enabled_repos = None):
        """
        Effective repository interface loader. Stay away from here.
        """

        if _enabled_repos is None:
            _enabled_repos = self._enabled_repos

        repo_data = self._settings['repositories']['available']
        if (repoid not in _enabled_repos) and \
            (not repo_data.get(repoid, {}).get('__temporary__')) and \
            (repoid not in repo_data):

            t = "%s: %s" % (_("bad repository id specified"), repoid,)
            if repoid not in self._repo_error_messages_cache:
                self.output(
                    darkred(t),
                    importance = 2,
                    level = "warning"
                )
                self._repo_error_messages_cache.add(repoid)
            raise RepositoryError("invalid repository id (1)")

        try:
            repo_obj = repo_data[repoid]
        except KeyError:
            raise RepositoryError("invalid repository id (2)")

        if repo_obj.get('__temporary__'):
            repo_key = self.__get_repository_cache_key(repoid)
            try:
                conn = self._memory_db_instances[repo_key]
            except KeyError:
                raise RepositoryError("invalid repository id (3)")
        else:
            dbfile = os.path.join(repo_obj['dbpath'],
                etpConst['etpdatabasefile'])
            if not os.path.isfile(dbfile):
                t = _("Repository %s hasn't been downloaded yet.") % (repoid,)
                if repoid not in self._repo_error_messages_cache:
                    if not etpUi['quiet']:
                        # don't want to have it printed if --quiet is on
                        self.output(
                            darkred(t),
                            importance = 2,
                            level = "warning"
                        )
                    self._repo_error_messages_cache.add(repoid)
                raise RepositoryError("repository not downloaded")

            conn = self.get_repository(repoid)(
                readOnly = True,
                dbFile = dbfile,
                name = repoid,
                xcache = xcache,
                indexing = indexing
            )
            conn.setCloseToken(repoid)
            self._add_plugin_to_client_repository(conn)

        if (repoid not in self._treeupdates_repos) and \
            entropy.tools.is_root() and \
            not self._is_package_repository(repoid):

            # only as root due to Portage
            try:
                updated = self.repository_packages_spm_sync(repoid, conn)
            except (OperationalError, DatabaseError,):
                updated = False
            if updated:
                self._cacher.discard()
                EntropyCacher.clear_cache_item(
                    EntropyCacher.CACHE_IDS['world_update'])
                EntropyCacher.clear_cache_item(
                    EntropyCacher.CACHE_IDS['critical_update'])
        return conn

    def get_repository_revision(self, reponame):
        """ @deprecated """
        try:
            return int(self.get_repository(reponame).revision(reponame))
        except (ValueError, TypeError,):
            return -1

    def add_repository(self, repository_metadata):
        """
        Add repository to Entropy Client configuration and data structures.
        NOTE: this method is NOT thread-safe.
        TODO: document metadata structure

        @param repository_metadata: repository metadata dict. See
            SystemSettings()['repositories']['available'][repository_id]
            for example metadata.
        @type repository_metadata: dict
        @return: True, if repository has been added
        @rtype: bool
        """
        avail_data = self._settings['repositories']['available']
        repoid = repository_metadata['repoid']

        avail_data[repoid] = {}
        avail_data[repoid]['description'] = repository_metadata['description']
        is_temp = repository_metadata.get('__temporary__')

        added = False
        if self._is_package_repository(repoid) or is_temp:
            # package repository

            # remove cache, if any
            self._settings._clear_repository_cache(repoid = repoid)

            avail_data[repoid]['plain_packages'] = \
                repository_metadata.get('plain_packages', [])[:]
            avail_data[repoid]['packages'] = \
                repository_metadata['packages'][:]
            smart_package = repository_metadata.get('smartpackage')
            if smart_package != None:
                avail_data[repoid]['smartpackage'] = smart_package

            avail_data[repoid]['post_branch_upgrade_script'] = \
                repository_metadata.get('post_branch_upgrade_script')
            avail_data[repoid]['post_repo_update_script'] = \
                repository_metadata.get('post_repo_update_script')
            avail_data[repoid]['post_branch_hop_script'] = \
                repository_metadata.get('post_branch_hop_script')
            avail_data[repoid]['dbpath'] = repository_metadata.get('dbpath')
            avail_data[repoid]['pkgpath'] = repository_metadata.get('pkgpath')
            avail_data[repoid]['__temporary__'] = repository_metadata.get(
                '__temporary__')
            avail_data[repoid]['webinstall_package'] = repository_metadata.get(
                'webinstall_package', False)
            avail_data[repoid]['webservices_config'] = repository_metadata.get(
                'webservices_config', None)
            # put at top priority, shift others
            self._settings['repositories']['order'].insert(0, repoid)
            # NOTE: never call SystemSettings.clear() here, or
            # ClientSystemSettingsPlugin.repositories_parser() will explode
            added = True

        else:

            # validate only in this case, because .etp ~ :, in other words,
            # package repos, won't pass the check anyway.
            if not entropy.tools.validate_repository_id(repoid):
                raise SecurityError("invalid repository identifier")

            added = self.__conf_add_remove_repository(
                repoid, repository_metadata)
            self._settings._clear_repository_cache(repoid = repoid)
            self.close_repositories()
            self.clear_cache()
            self._settings.clear()

        self._validate_repositories()
        return added

    def remove_repository(self, repository_id, disable = False):
        """
        Remove repository from Entropy Client configuration and data structures,
        if available.
        NOTE: this method is NOT thread-safe.

        @param repository_id: repository identifier
        @type repository_id: string
        @keyword disable: instead of removing the repository from entropy
            configuration, just disable it. (default is remove)
        @type disable: bool
        @return: True, if repository has been removed
        @rtype: bool
        """
        done = False
        removed_data = None
        if repository_id in self._settings['repositories']['available']:
            removed_data = self._settings['repositories']['available'].pop(
                repository_id)
            done = True

        if repository_id in self._settings['repositories']['excluded']:
            removed_data = self._settings['repositories']['excluded'].pop(
                repository_id)
            done = True

        # also early remove from _enabled_repos to avoid
        # issues when reloading SystemSettings which is bound to
        # Entropy Client SystemSettings plugin, which
        # triggers calculate_world_updates,
        # which triggers _all_repositories_hash, which triggers
        # open_repository, which triggers _load_repository,
        # which triggers an unwanted
        # output message => "bad repository id specified"
        if repository_id in self._enabled_repos:
            self._enabled_repos.remove(repository_id)

        try:
            self._settings['repositories']['order'].remove(
                repository_id)
        except ValueError:
            pass

        repo_key = self.__get_repository_cache_key(repository_id)
        try:
            dbconn = self._repodb_cache.pop(repo_key)
        except KeyError:
            dbconn = None

        if done:

            # drop from SystemSettings Client plugin, if there
            try:
                self.sys_settings_client_plugin._drop_package_repository(
                    repository_id)
            except KeyError:
                pass

            # if it's a package repository, don't remove cache here
            if not self._is_package_repository(repository_id):
                self._settings._clear_repository_cache(repoid = repository_id)
                # save new self._settings['repositories']['available'] to file
                # -- nothing to do anyway if repository is a package repository
                if disable:
                    done = self.__conf_enable_disable_repository(
                        repository_id, False)
                else:
                    done = self.__conf_add_remove_repository(
                        repository_id, None)
            self._settings.clear()

        mem_inst = self._memory_db_instances.pop(repo_key, None)
        if isinstance(mem_inst, EntropyRepositoryBase):
            mem_inst.close()

        if dbconn is not None:
            try:
                dbconn.close(_token = repository_id)
            except OperationalError:
                pass

        return done

    def __conf_enable_disable_repository(self, repository_id, enable):
        """
        Enable or disable given repository from Entropy configuration
        files.

        @param repository_id: repository identifier
        @type repository_id: string
        @param enable: True if enable, False if disable
        @type enable: bool
        @return: True, if action has been completed successfully
        @rtype: bool
        @raise IOError: if there are problems parsing config files
        """
        repo_d_conf = self._settings.get_setting_dirs_data(
            )['repositories_conf_d']
        conf_d_dir, conf_files_mtime, skipped_files, auto_upd = repo_d_conf
        repo_confs = [conf_p for conf_p, mtime_p in conf_files_mtime]
        # as per specifications, enabled config files handled by
        # Entropy Client (see repositories.conf.d/README) start with
        # entropy_ prefix.
        base_name = "entropy_" + repository_id
        enabled_conf_file = os.path.join(conf_d_dir, base_name)
        # while disabled config files start with _
        disabled_conf_file = os.path.join(conf_d_dir, "_" + base_name)

        # backward compatibility, handle repositories.conf
        repo_conf = self._settings.get_setting_files_data()['repositories']
        content = []
        enc = etpConst['conf_encoding']
        try:
            with codecs.open(repo_conf, encoding=enc) as f:
                content += [x.strip() for x in f.readlines()]
        except IOError as err:
            if err.errno == errno.EPERM:
                return False
            if err.errno != errno.ENOENT:
                raise

        accomplished = False
        new_content = []
        for line in content:
            key, value = entropy.tools.extract_setting(line)
            if key is None:
                new_content.append(line)
                continue

            key = key.replace(" ", "")
            key = key.replace("\t", "")
            line_repository_id = value.split("|")[0].strip()
            if (key == "repository") and (not enable) and \
                    (repository_id == line_repository_id):
                new_content.append("# repository = %s" % (value,))
                accomplished = True
                continue

            if key in ("#repository", "##repository") and enable and \
                    (repository_id == line_repository_id):
                new_content.append("repository = %s" % (value,))
                accomplished = True
                continue
            # generic line, add and forget
            new_content.append(line)
        content = new_content

        # enabling or disabling the repo is just a rename()
        # away for the new style files in repositories.conf.d/
        found_in_confd = False
        if enable:
            src = disabled_conf_file
            dest = enabled_conf_file
        else:
            src = enabled_conf_file
            dest = disabled_conf_file
        try:
            os.rename(src, dest)
            accomplished = True
            found_in_confd = True
        except OSError as err:
            if err.errno != errno.ENOENT:
                # do not handle EPERM ?
                raise

        if enable and found_in_confd:
            # if the action is enable and the repository
            # has been found in repositories.conf.d/
            # avoid updating repositories.conf
            pass
        else:
            # otherwise, go ahead and write the new content
            entropy.tools.atomic_write(
                repo_conf, "\n".join(content) + "\n", enc)

        return accomplished

    def __conf_add_remove_repository(self, repository_id, add_metadata):
        """
        Add or remove given repository from Entropy configuration
        files.

        @param repository_id: repository identifier
        @type repository_id: string
        @param add_metadata: if not None, the repository metadata
        is being used to build lines to add to config files. If None,
        the repository_id will be removed from entropy config, if possible.
        Repositories in repositories.conf.d/ handled outside Entropy (files not
        starting with "entropy_<repository-id>") won't be touched.
        @type add_metadata: dict
        @return: True, if action has been completed successfully
        @rtype: bool
        @raise IOError: if there are problems parsing config files
        """
        repo_d_conf = self._settings.get_setting_dirs_data(
            )['repositories_conf_d']
        conf_d_dir, conf_files_mtime, skipped_files, auto_upd = repo_d_conf
        repo_confs = [conf_p for conf_p, mtime_p in conf_files_mtime]
        # as per specifications, enabled config files handled by
        # Entropy Client (see repositories.conf.d/README) start with
        # entropy_ prefix.
        base_name = "entropy_" + repository_id
        enabled_conf_file = os.path.join(conf_d_dir, base_name)
        # while disabled config files start with _
        disabled_conf_file = os.path.join(conf_d_dir, "_" + base_name)

        # backward compatibility, handle repositories.conf
        repo_conf = self._settings.get_setting_files_data()['repositories']
        content = []
        enc = etpConst['conf_encoding']
        try:
            with codecs.open(repo_conf, encoding=enc) as f:
                content += [x.strip() for x in f.readlines()]
        except IOError as err:
            if err.errno == errno.EPERM:
                return False
            if err.errno != errno.ENOENT:
                raise

        # filter out current entry from content
        accomplished = False
        new_content = []
        for line in content:
            key, value = entropy.tools.extract_setting(line)
            if key is None:
                new_content.append(line)
                continue

            key = key.replace(" ", "")
            key = key.replace("\t", "")

            r_value = value.split("|")[0].strip()
            if (key == "repository") and (r_value == repository_id):
                if add_metadata is None:
                    accomplished = True
                continue
            if add_metadata is None:
                # remove mode, also drop repository lines
                # with "repository =" commented out
                if key in ("#repository", "##repository") and \
                        (r_value == repository_id):
                    accomplished = True
                    continue
            new_content.append(line)
        content = new_content

        if add_metadata is None:
            # remove mode
            try:
                os.remove(enabled_conf_file)
                accomplished = True
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise
            # since we want to remove, also drop disabled
            # config files
            try:
                os.remove(disabled_conf_file)
                accomplished = True
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise

        else:
            # add mode
            repository_lines = []
            mirror_count = 0
            for mirror in add_metadata['plain_packages']:
                if mirror_count == 0:
                    mirror_count += 1
                    rline = "repository = %s|%s|%s|%s#%s" % (
                        add_metadata['repoid'],
                        add_metadata['description'],
                        mirror,
                        add_metadata['plain_database'],
                        add_metadata['dbcformat'],
                    )
                else:
                    rline = "repository = %s||%s|" % (
                        add_metadata['repoid'],
                        mirror,
                    )
                repository_lines.append(rline)
            accomplished = True

            # create (overwrite) enabled_conf_file
            # containing proper repository_lines
            entropy.tools.atomic_write(
                enabled_conf_file, "\n".join(repository_lines) + "\n", enc)

            # if any disabled entry file is around, kill it with fire!
            try:
                os.remove(disabled_conf_file)
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise

        # commit back changes to config file in both cases.
        # Add: we migrate to the new config file automatically
        # Remove: we commit the filtered out content
        entropy.tools.atomic_write(
            repo_conf, "\n".join(content) + "\n", enc)

        return accomplished

    def __write_ordered_repositories_entries(self, ordered_repository_list):
        repo_conf = self._settings.get_setting_files_data()['repositories']
        content = []
        enc = etpConst['conf_encoding']
        try:
            with codecs.open(repo_conf, encoding=enc) as f:
                content += [x.strip() for x in f.readlines()]
        except IOError as err:
            if err.errno != errno.ENOENT:
                raise

        repolines = []
        filter_lines = []
        repolines_map = {}
        for line in content:
            key, value = entropy.tools.extract_setting(line)
            if key is not None:
                key = key.replace(" ", "")
                key = key.replace("\t", "")
                if key in ("repository", "#repository", "##repository"):
                    repolines.append(value)
                    if line not in filter_lines:
                        filter_lines.append(line)
                    repolines_map[value] = line

        content = [x for x in content if x not in filter_lines]
        for repoid in ordered_repository_list:
            for x in repolines:
                repoidline = x.split("|")[0].strip()
                if (repoid == repoidline) and (x in repolines_map):
                    line = repolines_map[x]
                    content.append(line)
                    if line in filter_lines:
                        filter_lines.remove(line)

        # write the rest of commented repolines
        for x in filter_lines:
            content.append(x)

        # atomic write
        entropy.tools.atomic_write(
            repo_conf, "\n".join(content) + "\n", enc)

    def shift_repository(self, repository_id, new_position_idx):
        """
        Change repository priority, move to given index "new_position_idx".
        The reference ordered list is at
        SystemSettings()['repositories']['order']
        NOTE: this method is NOT thread-safe.

        @param repository_id: repository identifier
        @type repository_id: string
        @param new_position_idx: new ordered list index
        @type new_position_idx: int
        @raise ValueError: if repository_id is invalid
        """
        # update self._settings['repositories']['order']
        self._settings['repositories']['order'].remove(repository_id)
        self._settings['repositories']['order'].insert(new_position_idx,
            repository_id)
        self.__write_ordered_repositories_entries(
            self._settings['repositories']['order'])
        self._settings.clear()
        self.close_repositories()
        self._settings._clear_repository_cache(repoid = repository_id)
        self._validate_repositories()

    def enable_repository(self, repository_id):
        """
        Enable given repository in Entropy Client configuration. If
        repository_id doesn't exist, nothing will change. But please, make
        sure this won't happen.
        NOTE: this method is NOT thread-safe.

        @param repository_id: repository identifier
        @type repository_id: string
        @return: True, if repository has been enabled
        @rtype: bool
        """
        self._settings._clear_repository_cache(repoid = repository_id)
        # save new self._settings['repositories']['available'] to file
        enabled = self.__conf_enable_disable_repository(repository_id, True)
        self._settings.clear()
        self.close_repositories()
        self._validate_repositories()
        return enabled

    def disable_repository(self, repository_id):
        """
        Disable given repository in Entropy Client configuration. If
        repository_id doesn't exist, nothing will change. But please, make
        sure this won't happen.
        NOTE: this method is NOT thread-safe.

        @param repository_id: repository identifier
        @type repository_id: string
        @raise ValueError: if repository is not available
        @raise ValueError: if repository is a package repository
        @return: True, if repository has been disabled, False otherwise
        @rtype: bool
        """
        # package repositories are ignored.
        if self._is_package_repository(repository_id):
            raise ValueError("repository is a package")

        # update self._settings['repositories']['available']
        try:
            del self._settings['repositories']['available'][repository_id]
        except KeyError:
            raise ValueError("repository identifier is not available")

        try:
            self._settings['repositories']['order'].remove(repository_id)
        except ValueError:
            raise ValueError("repository identifier is not available (2)")
        # it's not vital to reset
        # self._settings['repositories']['order'] counters

        self._settings._clear_repository_cache(repoid = repository_id)
        # save new self._settings['repositories']['available'] to file
        disabled = self.__conf_enable_disable_repository(repository_id, False)
        self._settings.clear()

        self.close_repositories()
        self._validate_repositories()
        return disabled

    def add_package_to_repositories(self, pkg_file):
        """@deprecated please use add_package_repository"""
        try:
            package_matches = self.add_package_repository(pkg_file)
            return 0, package_matches
        except EntropyPackageException:
            return -1, []

    def add_package_repository(self, package_file_path):
        """
        Add a package repository (through its package file) to Entropy Client.
        This is temporary and the lifecycle of it being available within
        Entropy Client is limited to this process lifetime.
        Any package file, either smart package or simple or webinstall must
        pass from here in order to get inserted, properly validated and
        made available.

        @param package_file_path: path to entropy package repository file
        @type package_file_path: string
        @return: list of package matches found in package repository.
        @raise entropy.exceptions.EntropyPackageException: if package file
            doesn't contain a valid package repository.
        """
        basefile = os.path.basename(package_file_path)
        db_dir = tempfile.mkdtemp()
        dbfile = os.path.join(db_dir, etpConst['etpdatabasefile'])
        dump_rc = entropy.tools.dump_entropy_metadata(package_file_path, dbfile)
        if not dump_rc:
            raise EntropyPackageException("repository metadata not found")

        webinstall_package = False
        if package_file_path.endswith(etpConst['packagesext_webinstall']):
            webinstall_package = True
            # unbzip2
            tmp_fd, tmp_path = tempfile.mkstemp(dir = db_dir)
            try:
                entropy.tools.uncompress_file(dbfile, tmp_path, bz2.BZ2File)
            finally:
                os.close(tmp_fd)
            os.rename(tmp_path, dbfile)

        repo = self.open_generic_repository(dbfile)
        # read all idpackages
        try:
            # all branches admitted from external files
            package_ids = repo.listAllPackageIds()
        except (AttributeError, DatabaseError, IntegrityError,
            OperationalError,):
            raise EntropyPackageException("corrupted repository")

        product = self._settings['repositories']['product']
        repodata = {}
        repodata['repoid'] = basefile
        repodata['description'] = "Dynamic Entropy Repository " + basefile
        repodata['dbpath'] = os.path.dirname(dbfile)
        # extra info added
        repodata['pkgpath'] = os.path.realpath(package_file_path)
        repodata['smartpackage'] = False # extra info added
        repodata['webinstall_package'] = webinstall_package
        repodata['packages'] = []
        repodata['plain_packages'] = []
        repodata['post_branch_upgrade_script'] = None
        repodata['post_repo_update_script'] = None
        repodata['post_branch_hop_script'] = None
        repodata['webservices_config'] = None
        if repodata['webinstall_package']:
            try:
                plain_packages = repo.getSetting("plain_packages")
            except KeyError:
                plain_packages = None
            if plain_packages is not None:
                repodata['plain_packages'] = plain_packages.split("\n")

        if len(package_ids) > 1:
            repodata['smartpackage'] = True
        is_webinstall_pkg = repodata['webinstall_package']

        try:
            compiled_arch = repo.getSetting("arch")
        except KeyError:
            compiled_arch = None

        package_matches = []
        if compiled_arch is not None:
            # new way of checking repo architecture
            if compiled_arch != etpConst['currentarch']:
                raise EntropyPackageException("invalid architecture")

            if is_webinstall_pkg:
                for package_id in package_ids:
                    source = repo.getInstalledPackageSource(package_id)
                    if source != etpConst['install_sources']['user']:
                        continue
                    package_matches.append((package_id, basefile))
            else:
                package_matches.extend([(package_id, basefile) for package_id in
                    package_ids])
        else:
            # old, legacy (broken) way
            for package_id in package_ids:
                compiled_arch = repo.retrieveDownloadURL(package_id)
                if compiled_arch.find("/"+etpConst['currentarch']+"/") == -1:
                    raise EntropyPackageException("invalid architecture")
                if is_webinstall_pkg:
                    source = repo.getInstalledPackageSource(package_id)
                    if source != etpConst['install_sources']['user']:
                        continue
                    # otherwise, add to package_matches
                package_matches.append((package_id, basefile))

        added = self.add_repository(repodata)
        if not added:
            raise EntropyPackageException("error while adding repository (1)")
        self._validate_repositories()
        if basefile not in self._enabled_repos:
            self.remove_repository(basefile) # ignored outcome
            raise EntropyPackageException("error while adding repository (2)")

        # add to SystemSettings
        self.sys_settings_client_plugin._add_package_repository(
            repodata['repoid'], repodata)
        repo.close()
        return package_matches

    def _add_plugin_to_client_repository(self, entropy_client_repository):
        etp_db_meta = {
            'output_interface': self,
        }
        repo_plugin = ClientEntropyRepositoryPlugin(self,
            metadata = etp_db_meta)
        entropy_client_repository.add_plugin(repo_plugin)

    def repositories(self):
        """
        Return a list of enabled (and valid) repository identifiers, excluding
        installed packages repository. You can use the identifiers in this list
        to open EntropyRepository instances using Client.open_repository()
        NOTE: this method directly returns a reference to the internal
        enabled repository list object.
        NOTE: the returned list is built based on SystemSettings repository
        metadata but might differ because extra checks are done at runtime.
        So, if you want to iterate over valid repositories, use this method.

        @return: enabled and valid repository identifiers
        @rtype list
        """
        return self._enabled_repos

    def installed_repository(self):
        """
        Return Entropy Client installed packages repository.

        @return: Entropy Client installed packages repository
        @rtype: entropy.db.EntropyRepository
        """
        return self._installed_repository

    def _open_installed_repository(self):

        def load_db_from_ram():
            self.safe_mode = etpConst['safemodeerrors']['clientdb']
            mytxt = "%s, %s" % (_("System database not found or corrupted"),
                _("running in safe mode using temporary, empty repository"),)
            self.output(
                darkred(mytxt),
                importance = 1,
                level = "warning",
                header = bold(" !!! "),
            )
            m_conn = self.open_temp_repository(name = etpConst['clientdbid'])
            self._add_plugin_to_client_repository(m_conn)
            return m_conn

        db_dir = os.path.dirname(etpConst['etpdatabaseclientfilepath'])
        if not os.path.isdir(db_dir):
            os.makedirs(db_dir)

        db_path = etpConst['etpdatabaseclientfilepath']
        if (self._installed_repo_enable) and (not os.path.isfile(db_path)):
            conn = load_db_from_ram()
            entropy.tools.print_traceback(f = self.logger)
        else:
            try:
                repo_class = self.get_repository(etpConst['clientdbid'])
                conn = repo_class(readOnly = False,
                    dbFile = db_path,
                    name = etpConst['clientdbid'],
                    xcache = self.xcache, indexing = self._indexing
                )
                conn.setCloseToken(etpConst['clientdbid'])
                self._add_plugin_to_client_repository(conn)
                # TODO: remove this in future, drop useless data from clientdb
            except (DatabaseError,):
                entropy.tools.print_traceback(f = self.logger)
                conn = load_db_from_ram()
            else:
                # validate database
                if self._installed_repo_enable:
                    try:
                        conn.validate()
                    except SystemDatabaseError:
                        try:
                            conn.close(_token = etpConst['clientdbid'])
                        except (RepositoryPluginError, OSError, IOError):
                            pass
                        entropy.tools.print_traceback(f = self.logger)
                        conn = load_db_from_ram()

        self._installed_repository = conn
        return conn

    def reopen_installed_repository(self):
        """
        If for whatever reason (usually there is NO reason!) the installed
        packages repository needs to be reloaded, call this method.
        """
        self._installed_repository.close(_token = etpConst['clientdbid'])
        self._open_installed_repository()
        # make sure settings are in sync
        self._settings.clear()

    def open_generic_repository(self, repository_path, dbname = None,
        name = None, xcache = None, read_only = False, indexing_override = None,
        skip_checks = False):
        """
        Open a Generic Entropy Repository interface, using
        entropy.client.interfaces.db.GenericRepository class.

        @param repository_path: path to valid Entropy Repository file
        @type repository_path: string
        @keyword dbname: backward compatibility, don't use this
        @type dbname: string
        @keyword name: repository identifier hold by the repository object and
            returned by repository_id()
        @type name: string
        @keyword xcache: enable on-disk cache for repository?
        @type xcache: bool
        @keyword read_only: True, will keep the repository read-only, rolling
            back any transaction
        @type read_only: bool
        @keyword indexing_override: override default indexing settings (default
            is disabled for this kind of, usually small, repositories)
        @type indexing_override: bool
        @keyword skip_checks: skip integrity checks on repository
        @type skip_checks: bool
        @return: a GenericRepository object
        @rtype: entropy.client.interfaces.db.GenericRepository
        """
        if xcache is None:
            xcache = self.xcache
        if indexing_override != None:
            indexing = indexing_override
        else:
            indexing = self._indexing
        if dbname is not None:
            # backward compatibility
            name = dbname
        conn = GenericRepository(
            readOnly = read_only,
            dbFile = repository_path,
            name = name,
            xcache = xcache,
            indexing = indexing,
            skipChecks = skip_checks
        )
        self._add_plugin_to_client_repository(conn)
        return conn

    def open_temp_repository(self, dbname = None, name = None,
        temp_file = None):
        """
        Open a temporary (using mkstemp()) Entropy Repository.
        Indexing and Caching are disabled by default.

        @keyword dbname: backward compatibility, don't use this
        @type dbname: string
        @keyword name: repository identifier hold by the repository object and
            returned by repository_id()
        @type name: string
        @keyword temp_file: override random temporary file and open given
            temp_file. No path validity check will be run.
        @type temp_file: string
        @return: a GenericRepository object
        @rtype: entropy.client.interfaces.db.GenericRepository
        """
        if temp_file is None:
            tmp_fd, temp_file = tempfile.mkstemp(
                prefix="entropy.client.methods.open_temp_repository")
            os.close(tmp_fd)
        if dbname is not None:
            # backward compatibility
            name = dbname
        dbc = GenericRepository(
            readOnly = False,
            dbFile = temp_file,
            name = name,
            xcache = False,
            indexing = False,
            skipChecks = True,
            temporary = True
        )
        self._add_plugin_to_client_repository(dbc)
        dbc.initializeRepository()
        return dbc

    def backup_repository(self, repository_id, backup_dir, silent = False,
        compress_level = 9):
        """
        Backup given repository into given backup directory.

        @param repository_id: repository identifier
        @type repository_id: string
        @param backup_dir: backup directory
        @type backup_dir: string
        @keyword silent: execute in silent mode if True
        @type silent: bool
        @keyword compress_level: compression level, range from 1 to 9
        @type compress_level: int
        """
        if compress_level not in range(1, 10):
            compress_level = 9

        def get_ts():
            ts = datetime.fromtimestamp(time.time())
            return "%s%s%s_%sh%sm%ss" % (ts.year, ts.month, ts.day, ts.hour,
                ts.minute, ts.second)

        backup_name = "%s%s.%s.backup" % (etpConst['dbbackupprefix'],
            repository_id, get_ts(),)
        backup_path = os.path.join(backup_dir, backup_name)
        comp_backup_path = backup_path + ".bz2"

        repo_db = self.open_repository(repository_id)
        if not silent:
            mytxt = "%s: %s ..." % (
                darkgreen(_("Backing up repository to")),
                blue(os.path.basename(comp_backup_path)),
            )
            self.output(
                mytxt,
                importance = 1,
                level = "info",
                header = blue(" @@ "),
                back = True
            )
        f_out = bz2.BZ2File(comp_backup_path, "wb")
        try:
            repo_db.exportRepository(f_out)
        except DatabaseError as err:
            return False, err
        finally:
            f_out.close()

        if not silent:
            mytxt = "%s: %s" % (
                darkgreen(_("Repository backed up successfully")),
                blue(os.path.basename(comp_backup_path)),
            )
            self.output(
                mytxt,
                importance = 1,
                level = "info",
                header = blue(" @@ ")
            )
        return True, _("All fine")

    def restore_repository(self, backup_path, repository_path,
        repository_id, silent = False):
        """
        Restore given repository.

        @param backup_path: path to backed up repository file
        @type backup_path: string
        @param repository_path: repository destination path
        @type repository_path: string
        @param repository_id: repository identifier
        @type repository_id: string
        @keyword silent: execute in silent mode if True
        @type silent: bool
        """
        # uncompress the backup
        if not silent:
            mytxt = "%s: %s => %s ..." % (
                darkgreen(_("Restoring backed up repository")),
                blue(os.path.basename(backup_path)),
                blue(repository_path),
            )
            self.output(
                mytxt,
                importance = 1,
                level = "info",
                header = blue(" @@ "),
                back = True
            )
        uncompressed_backup_path = backup_path[:-len(".bz2")]
        try:
            entropy.tools.uncompress_file(backup_path, uncompressed_backup_path,
                bz2.BZ2File)
        except (IOError, OSError):
            if not silent:
                entropy.tools.print_traceback()
            return False, _("Unable to unpack")

        repo_class = self.get_repository(repository_id)
        try:
            repo_class.importRepository(uncompressed_backup_path,
                repository_path)
        finally:
            os.remove(uncompressed_backup_path)
        if not silent:
            mytxt = "%s: %s" % (
                darkgreen(_("Repository restored successfully")),
                blue(os.path.basename(backup_path)),
            )
            self.output(
                mytxt,
                importance = 1,
                level = "info",
                header = blue(" @@ ")
            )

        self.clear_cache()
        return True, _("All fine")

    def installed_repository_backups(self, repository_directory = None):
        """
        List available backups for the installed packages repository.

        @keyword repository_directory: alternative backup directory
        @type repository_directory: string
        @return: list of paths
        @rtype: list
        """
        if repository_directory is None:
            repository_directory = os.path.dirname(
                etpConst['etpdatabaseclientfilepath'])

        valid_backups = []
        for fname in os.listdir(repository_directory):
            if not fname.startswith(etpConst['dbbackupprefix']):
                continue
            path = os.path.join(repository_directory, fname)
            if not os.path.isfile(path):
                continue
            if not os.access(path, os.R_OK):
                continue
            valid_backups.append(path)
        return valid_backups

    def clean_downloaded_packages(self, dry_run = False, days_override = None):
        """
        Clean Entropy Client downloaded packages older than the setting
        specified by "packages-autoprune-days" in /etc/entropy/client.conf.
        If setting is not set or invalid, this method will do nothing.
        Otherwise, files older than given settings (representing time delta in
        days) will be removed.

        @keyword dry_run: do not remove files, just return them
        @type dry_run: bool
        @keyword days_override: override SystemSettings setting (from client.conf)
        @type days_override: int
        @return: list of removed package file paths.
        @rtype: list
        @raise AttributeError: if days_override or client.conf setting is
            invalid (the latter cannot really happen).
        """
        client_settings = self._settings[self.sys_settings_client_plugin_id]
        misc_settings = client_settings['misc']
        autoprune_days = misc_settings.get('autoprune_days', days_override)
        if autoprune_days is None:
            # sorry, feature disabled or not available
            return []
        if not const_isnumber(autoprune_days):
            raise AttributeError("autoprune_days is invalid")

        def filter_expired_pkg(pkg_path):

            if not os.path.isfile(pkg_path):
                return False
            if not os.access(pkg_path, os.R_OK | os.W_OK):
                return False
            try:
                mtime = os.path.getmtime(pkg_path)
            except (OSError, IOError):
                return False
            if (mtime + (autoprune_days*24*3600)) > time.time():
                return False
            return True

        repo_pkgs_dirs = [os.path.join(etpConst['entropypackagesworkdir'], x,
            etpConst['currentarch']) for x in \
                etpConst['packagesrelativepaths']]

        def get_removable_packages():
            removable_pkgs = set()
            for pkg_dir in repo_pkgs_dirs:
                try:
                    pkg_dir_list = os.listdir(pkg_dir)
                except OSError as err:
                    if err.errno not in (errno.ENOTDIR, errno.ENOENT):
                        raise
                    # pkg_dir is not a dir or doesn't exist
                    continue
                for branch in pkg_dir_list:
                    branch_dir = os.path.join(pkg_dir, branch)
                    try:
                        dir_repo_pkgs = set((os.path.join(branch_dir, x) \
                            for x in os.listdir(branch_dir)))
                    except OSError as err:
                        if err.errno not in (errno.ENOTDIR, errno.ENOENT):
                            raise
                        # branch_dir is not a dir or doesn't exist
                        continue
                    # filter out hostile paths
                    dir_repo_pkgs = set((x for x in dir_repo_pkgs \
                        if os.path.realpath(x).startswith(branch_dir) \
                        and os.path.realpath(x).endswith(
                            etpConst['packagesext'])))
                    removable_pkgs |= dir_repo_pkgs
            return removable_pkgs

        removable_pkgs = get_removable_packages()
        removable_pkgs = sorted(filter(filter_expired_pkg,
            removable_pkgs))

        if not removable_pkgs:
            return []
        if dry_run:
            return removable_pkgs

        successfully_removed = []
        for repo_pkg in removable_pkgs:

            mytxt = "%s: %s" % (
                blue(_("Removing")),
                purple(repo_pkg),
            )
            self.output(
                mytxt,
                importance = 1,
                level = "info",
                header = purple(" @@ ")
            )

            try:
                os.remove(repo_pkg)
                successfully_removed.append(repo_pkg)
            except OSError:
                pass

            try:
                os.remove(repo_pkg + etpConst['packagesmd5fileext'])
            except OSError:
                pass
            try:
                os.remove(repo_pkg + \
                    etpConst['packagemtimefileext'])
            except OSError:
                # KeyError is for backward compatibility
                pass

        return successfully_removed

    def _run_repositories_post_branch_switch_hooks(self, old_branch, new_branch):
        """
        This method is called whenever branch is successfully switched by user.
        Branch is switched when user wants to upgrade the OS to a new
        major release.
        Any repository can be shipped with a sh script which if available,
        handles system configuration to ease the migration.

        @param old_branch: previously set branch
        @type old_branch: string
        @param new_branch: newly set branch
        @type new_branch: string
        @return: tuple composed by (1) list of repositories whose script has
        been run and (2) bool describing if scripts exited with error
        @rtype: tuple(set, bool)
        """

        const_debug_write(__name__,
            "run_repositories_post_branch_switch_hooks: called")

        client_dbconn = self._installed_repository
        hooks_ran = set()
        if client_dbconn is None:
            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: clientdb not avail")
            return hooks_ran, True

        errors = False
        repo_data = self._settings['repositories']['available']
        repo_data_excl = self._settings['repositories']['available']
        all_repos = sorted(set(list(repo_data.keys()) + list(repo_data_excl.keys())))

        for repoid in all_repos:

            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: %s" % (
                    repoid,)
            )

            mydata = repo_data.get(repoid)
            if mydata is None:
                mydata = repo_data_excl.get(repoid)

            if mydata is None:
                const_debug_write(__name__,
                    "run_repositories_post_branch_switch_hooks: skipping %s" % (
                        repoid,)
                )
                continue

            branch_mig_script = mydata['post_branch_hop_script']
            if branch_mig_script is not None:
                branch_mig_md5sum = '0'
                if os.access(branch_mig_script, os.R_OK) and \
                    os.path.isfile(branch_mig_script):
                    branch_mig_md5sum = entropy.tools.md5sum(branch_mig_script)

            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: script md5: %s" % (
                    branch_mig_md5sum,)
            )

            # check if it is needed to run post branch migration script
            status_md5sums = client_dbconn.isBranchMigrationAvailable(
                repoid, old_branch, new_branch)
            if status_md5sums:
                if branch_mig_md5sum == status_md5sums[0]: # its stored md5
                    const_debug_write(__name__,
                        "run_repositories_post_branch_switch_hooks: skip %s" % (
                            branch_mig_script,)
                    )
                    continue # skipping, already ran the same script

            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: preparing run: %s" % (
                    branch_mig_script,)
                )

            if branch_mig_md5sum != '0':
                args = ["/bin/sh", branch_mig_script, repoid, 
                    etpConst['systemroot'] + "/", old_branch, new_branch]
                const_debug_write(__name__,
                    "run_repositories_post_branch_switch_hooks: run: %s" % (
                        args,)
                )
                proc = subprocess.Popen(args, stdin = sys.stdin,
                    stdout = sys.stdout, stderr = sys.stderr)
                # it is possible to ignore errors because
                # if it's a critical thing, upstream dev just have to fix
                # the script and will be automagically re-run
                br_rc = proc.wait()
                const_debug_write(__name__,
                    "run_repositories_post_branch_switch_hooks: rc: %s" % (
                        br_rc,)
                )
                if br_rc != 0:
                    errors = True

            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: done")

            # update metadata inside database
            # overriding post branch upgrade md5sum is INTENDED
            # here but NOT on the other function
            # this will cause the post-branch upgrade migration
            # script to be re-run also.
            client_dbconn.insertBranchMigration(repoid, old_branch, new_branch,
                branch_mig_md5sum, '0')

            const_debug_write(__name__,
                "run_repositories_post_branch_switch_hooks: db data: %s" % (
                    (repoid, old_branch, new_branch, branch_mig_md5sum, '0',),)
            )

            hooks_ran.add(repoid)

        return hooks_ran, errors

    def _run_repository_post_branch_upgrade_hooks(self, pretend = False):
        """
        This method is called whenever branch is successfully switched by user
        and all the updates have been installed (also look at:
        run_repositories_post_branch_switch_hooks()).
        Any repository can be shipped with a sh script which if available,
        handles system configuration to ease the migration.

        @param pretend: do not run hooks but just return list of repos whose
            scripts should be run
        @type pretend: bool
        @return: tuple of length 2 composed by list of repositories whose
            scripts have been run and errors boolean)
        @rtype: tuple
        """

        const_debug_write(__name__,
            "run_repository_post_branch_upgrade_hooks: called"
        )

        client_dbconn = self._installed_repository
        hooks_ran = set()
        if client_dbconn is None:
            return hooks_ran, True

        repo_data = self._settings['repositories']['available']
        branch = self._settings['repositories']['branch']
        errors = False

        for repoid in self._enabled_repos:

            const_debug_write(__name__,
                "run_repository_post_branch_upgrade_hooks: repoid: %s" % (
                    (repoid,),
                )
            )

            mydata = repo_data.get(repoid)
            if mydata is None:
                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: repo data N/A")
                continue

            # check if branch upgrade script exists
            branch_upg_script = mydata['post_branch_upgrade_script']
            branch_upg_md5sum = '0'
            if branch_upg_script is not None:
                if os.access(branch_upg_script, os.R_OK) and \
                    os.path.isfile(branch_upg_script):
                    branch_upg_md5sum = entropy.tools.md5sum(branch_upg_script)

            if branch_upg_md5sum == '0':
                # script not found, skip completely
                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                        repoid, "branch upgrade script not avail",)
                )
                continue

            const_debug_write(__name__,
                "run_repository_post_branch_upgrade_hooks: script md5: %s" % (
                    branch_upg_md5sum,)
            )

            upgrade_data = client_dbconn.retrieveBranchMigration(branch)
            if upgrade_data.get(repoid) is None:
                # no data stored for this repository, skipping
                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                        repoid, "branch upgrade data not avail",)
                )
                continue
            repo_upgrade_data = upgrade_data[repoid]

            const_debug_write(__name__,
                "run_repository_post_branch_upgrade_hooks: upgrade data: %s" % (
                    repo_upgrade_data,)
            )

            for from_branch in sorted(repo_upgrade_data):

                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: upgrade: %s" % (
                        from_branch,)
                )

                # yeah, this is run for every branch even if script
                # which md5 is checked against is the same
                # this makes the code very flexible
                post_mig_md5, post_upg_md5 = repo_upgrade_data[from_branch]
                if branch_upg_md5sum == post_upg_md5:
                    # md5 is equal, this means that it's been already run
                    const_debug_write(__name__,
                        "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                            "already run for from_branch", from_branch,)
                    )
                    continue

                hooks_ran.add(repoid)

                if pretend:
                    const_debug_write(__name__,
                        "run_repository_post_branch_upgrade_hooks: %s: %s => %s" % (
                            "pretend enabled, not actually running",
                            repoid, from_branch,
                        )
                    )
                    continue

                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                        "running upgrade script from_branch:", from_branch,)
                )

                args = ["/bin/sh", branch_upg_script, repoid,
                    etpConst['systemroot'] + "/", from_branch, branch]
                proc = subprocess.Popen(args, stdin = sys.stdin,
                    stdout = sys.stdout, stderr = sys.stderr)
                mig_rc = proc.wait()

                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                        "upgrade script exit status", mig_rc,)
                )

                if mig_rc != 0:
                    errors = True

                # save branch_upg_md5sum in db
                client_dbconn.setBranchMigrationPostUpgradeMd5sum(repoid,
                    from_branch, branch, branch_upg_md5sum)

                const_debug_write(__name__,
                    "run_repository_post_branch_upgrade_hooks: %s: %s" % (
                        "saved upgrade data",
                        (repoid, from_branch, branch, branch_upg_md5sum,),
                    )
                )

        return hooks_ran, errors


class MiscMixin:

    # resources lock file object container
    # RESOURCES_LOCK_F_REF = None
    # RESOURCES_LOCK_F_COUNT = 0
    # RESOURCES_LOCK_F_COUNT_MUTEX = threading.Lock()
    _FILE_LOCK_MUTEX = threading.Lock()
    _FILE_LOCK_MAP = {
    }

    def setup_file_permissions(self, file_path):
        """ @deprecated """
        const_setup_file(file_path, etpConst['entropygid'], 0o664)

    def _file_lock_setup(self, file_path):
        """
        Setup _FILE_LOCK_MAP for file_path, allocating locking information.
        """
        mapped = MiscMixin._FILE_LOCK_MAP.get(file_path)
        if mapped is None:
            mapped = {
                'count': 0,
                'ref': None,
                'path': file_path,
            }
            MiscMixin._FILE_LOCK_MAP[file_path] = mapped
        return mapped

    def _lock_resource(self, lock_path, blocking, shared):
        """
        Internal function that does the locking given a lock
        file path.
        """
        with MiscMixin._FILE_LOCK_MUTEX:
            mapped = self._file_lock_setup(lock_path)
            if mapped['ref'] is not None:
                # reentrant lock, already acquired
                return True
            acquired = self._file_lock_create(
                mapped, blocking = blocking, shared = shared)
            if acquired:
                mapped['count'] += 1
            return acquired

    def _promote_resource(self, lock_path, blocking):
        """
        Internal function that does the file lock promotion.
        """
        with MiscMixin._FILE_LOCK_MUTEX:
            mapped = self._file_lock_setup(lock_path)
            flock_f = mapped['ref']
            if flock_f is None:
                # wtf ?
                raise IOError("not acquired")
            acquired = True
            if blocking:
                flock_f.promote()
            else:
                acquired = flock_f.try_promote()
            return acquired

    def _unlock_resource(self, lock_path):
        """
        Internal function that does the unlocking of a given
        lock file.
        """
        with MiscMixin._FILE_LOCK_MUTEX:
            mapped = self._file_lock_setup(lock_path)
            # decrement lock counter
            if mapped['count'] > 0:
                mapped['count'] -= 1
            # if lock counter > 0, still locked
            # waiting for other upper-level calls
            if mapped['count'] > 0:
                return

            ref_obj = mapped['ref']
            if ref_obj is not None:
                try:
                    # unlink first
                    os.remove(ref_obj.get_file().name)
                except OSError as err:
                    # cope with possible race conditions
                    # and read-only filesystem
                    if err.errno not in (errno.ENOENT, errno.EROFS):
                        raise
                finally:
                    ref_obj.release()
                    ref_obj.close()
                    mapped['ref'] = None

    def _file_lock_create(self, lock_data, blocking = False, shared = False):
        """
        Create and allocate the lock file pointed by lock_data structure.
        """
        pidfile = lock_data['path']
        lockdir = os.path.dirname(pidfile)
        if not os.path.isdir(lockdir):
            os.makedirs(lockdir, 0o775)
        const_setup_perms(lockdir, etpConst['entropygid'], recursion = False)
        mypid = os.getpid()

        try:
            pid_f = open(pidfile, "a+")
        except IOError as err:
            if err.errno in (errno.ENOENT, errno.EACCES):
                # cannot get lock or dir doesn't exist
                return False
            raise

        # ensure that entropy group can write on that
        try:
            const_setup_file(pidfile, etpConst['entropygid'], 0o664)
        except OSError:
            pass

        flock_f = FlockFile(pidfile, fobj = pid_f)
        if blocking:
            if shared:
                flock_f.acquire_shared()
            else:
                flock_f.acquire_exclusive()
        else:
            acquired = False
            if shared:
                acquired = flock_f.try_acquire_shared()
            else:
                acquired = flock_f.try_acquire_exclusive()
            if not acquired:
                # lock already acquired, but might come from the same pid
                stored_pid = pid_f.read(128).strip()
                pid_f.close()
                if str(os.getpid()) == stored_pid:
                    # it's me, entropy (in case I called execv*)
                    # NOTE that this won't work with shared locks
                    return True
                return False

        if not shared:
            # if we acquired an exclusive lock
            # we can place our pid here.
            pid_f.truncate()
            pid_f.write(str(mypid))
            pid_f.flush()

        lock_data['ref'] = flock_f
        self._clear_resource_live_cache()
        return True

    def _clear_resource_live_cache(self):
        """
        Clear in-RAM cache after having acquired a resource lock.
        """
        with self._cacher:
            # clear repositories live cache
            if self._installed_repository is not None:
                self._installed_repository.clearCache()
            for repo in self._repodb_cache.values():
                repo.clearCache()
            self._settings.clear()
            self._cacher.discard()
        self._cacher.sync()

    def _wait_resource(self, lock_func, sleep_seconds = 1.0,
                       max_lock_count = 300, shared = False):
        """
        Poll on a given resource hoping to get its lock.
        """
        lock_count = 0
        # check lock file
        while True:
            acquired = lock_func(blocking=False, shared=shared)
            if acquired:
                if lock_count > 0:
                    self.output(
                        blue(_("Resources unlocked, let's go!")),
                        importance = 1,
                        level = "info",
                        header = darkred(" @@ ")
                    )
                break
            if lock_count >= max_lock_count:
                self.output(
                    blue(_("Resources still locked, giving up!")),
                    importance = 1,
                    level = "warning",
                    header = darkred(" @@ ")
                )
                return True # gave up
            lock_count += 1
            self.output(
                blue(_("Resources locked, sleeping...")),
                importance = 1,
                level = "warning",
                header = darkred(" @@ "),
                back = True,
                count = (lock_count, max_lock_count)
            )
            time.sleep(sleep_seconds)
        return False # yay!

    def lock_resources(self, blocking = False, shared = False):
        """
        Acquire Entropy Resources lock; once acquired, it's possible
        to alter:
        - Installed Packages Repository
        - Available Packages Repositories
        - Entropy Configuration and metadata
        If shared=True, you are likely calling this method as user, if
        so, make sure that the same is in the "entropy" group, by
        using entropy.tools.is_user_in_entropy_group().

        @keyword blocking: execute in blocking mode?
        @type blocking: bool
        @keyword shared: acquire a shared lock? (default is False)
        @type shared: bool
        @return: True, if lock has been acquired. False otherwise.
        @rtype: bool
        """
        lock_path = etpConst['locks']['using_resources']
        return self._lock_resource(lock_path, blocking, shared)

    def promote_resources(self, blocking = False):
        """
        Promote previously acquired Entropy Resources Lock from
        shared to exclusive.

        @keyword blocking: execute in blocking mode?
        @type blocking: bool
        """
        lock_path = etpConst['locks']['using_resources']
        return self._promote_resource(lock_path, blocking)

    def unlock_resources(self):
        """
        Release previously locked Entropy Resources, see lock_resources().
        """
        lock_path = etpConst['locks']['using_resources']
        return self._unlock_resource(lock_path)

    def wait_resources(self, sleep_seconds = 1.0, max_lock_count = 300,
                       shared = False):
        """
        Wait until Entropy resources are unlocked.
        This method polls over the available repositories lock and
        could run into starvation.

        @keyword sleep_seconds: time between checks
        type sleep_seconds: float
        @keyword max_lock_count: maximum number of times the lock is checked
        @type max_lock_count: int
        @keyword shared: acquire a shared lock? (default is False)
        @type shared: bool
        @return: True, if lock hasn't been released, False otherwise.
        @rtype: bool
        """
        return self._wait_resource(
            self.lock_resources, sleep_seconds=sleep_seconds,
            max_lock_count=max_lock_count, shared=shared)

    def switch_chroot(self, chroot):
        """
        Switch Entropy Client to work on given chroot.
        Please consider this method EXPERIMENTAL. No verification will
        be made against given "chroot" path.
        By default, chroot equals to "". So, to switch back to default chroot,
        please pass chroot="".

        @param chroot: path to new valid chroot
        @type chroot: string
        """
        self.clear_cache()
        self.close_repositories()
        if chroot.endswith("/"):
            chroot = chroot[:-1]
        etpSys['rootdir'] = chroot
        # reload constants
        initconfig_entropy_constants(etpSys['rootdir'])
        self._settings.clear()
        self._validate_repositories()
        self.reopen_installed_repository()
        # keep them closed, since SystemSettings.clear() is called
        # above on reopen_installed_repository()
        self.close_repositories()
        if chroot:
            try:
                self._installed_repository.resetTreeupdatesDigests()
            except EntropyRepositoryError:
                pass

    def is_entropy_package_free(self, package_id, repository_id):
        """
        Return whether given Entropy package match tuple points to a free
        (as in freedom) package.

        @param package_id: package identifier
        @type package_id: int
        @param repository_id: repository identifier
        @type repository_id: string
        @return: True, if given entropy package is free (as in freedom)
        @rtype: bool
        """
        cl_id = self.sys_settings_client_plugin_id
        repo_sys_data = self._settings[cl_id]['repositories']

        dbconn = self.open_repository(repository_id)

        wl = repo_sys_data['license_whitelist'].get(repository_id)
        if not wl: # no whitelist available
            return True

        keys = dbconn.retrieveLicenseDataKeys(package_id)
        keys = [x for x in keys if x not in wl]
        if keys:
            return False
        return True

    def get_licenses_to_accept(self, package_matches):
        """
        Return, for given package matches, what licenses have to be accepted.

        @param package_matches: list of entropy package matches
            [(package_id, repository_id), ...]
        @type package_matches: list
        @return: dictionary composed by license id as key and list of package
            matches as value.
        @rtype: dict
        """
        cl_id = self.sys_settings_client_plugin_id
        repo_sys_data = self._settings[cl_id]['repositories']
        lic_accepted = self._settings['license_accept']

        licenses = {}
        for pkg_id, repo_id in package_matches:
            dbconn = self.open_repository(repo_id)
            wl = repo_sys_data['license_whitelist'].get(repo_id)
            if not wl:
                continue
            try:
                keys = dbconn.retrieveLicenseDataKeys(pkg_id)
            except OperationalError:
                # it has to be fault-tolerant, cope with missing tables
                continue
            keys = [x for x in keys if x not in lic_accepted]
            for key in keys:
                if key in wl:
                    continue
                found = self._installed_repository.isLicenseAccepted(key)
                if found:
                    continue
                obj = licenses.setdefault(key, set())
                obj.add((pkg_id, repo_id))

        return licenses

    def reorder_mirrors(self, repository_id, dry_run = False):
        """
        Reorder mirror list for given repository using ping statistics.

        @param repository_id: repository identifier
        @type repository_id: string
        @keyword dry_run: do not actually change repository mirrors order
        @type dry_run: bool
        @raise KeyError: if repository_id is not available
        @return: new repository metadata
        @rtype: dict
        """
        repo_data = None
        avail_data = self._settings['repositories']['available']
        excluded_data = self._settings['repositories']['excluded']
        mirror_test_file = "MIRROR_TEST"

        if repository_id in avail_data:
            repo_data = avail_data[repository_id]
        elif repository_id in excluded_data:
            repo_data = excluded_data[repository_id]

        if repo_data is None:
            raise KeyError("repository_id not found")

        pkg_mirrors = repo_data['plain_packages']
        mirror_stats = {}
        mirror_cache = set()
        retries = 1

        for mirror in pkg_mirrors:

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="entropy.client.methods.reorder_mirrors")
            try:

                url_data = entropy.tools.spliturl(mirror)
                hostname = url_data.hostname
                if hostname in mirror_cache:
                    continue
                mirror_cache.add(hostname)
                mirror_url = mirror + "/" + mirror_test_file

                mytxt = "%s: %s" % (
                    blue(_("Checking speed of")),
                    purple(hostname),
                )
                self.output(
                    mytxt,
                    importance = 1,
                    level = "info",
                    header = purple(" @@ "),
                    back = True
                )

                download_speeds = []
                for idx in range(retries):
                    fetcher = self._url_fetcher(mirror_url, tmp_path,
                        resume = False, show_speed = False)
                    rc = fetcher.download()
                    if rc not in ("-3", "-4"):
                        download_speeds.append(fetcher.get_transfer_rate())

                result_speed = 0.0
                if download_speeds:
                    download_speeds.sort(reverse=True)
                    # take the best
                    result_speed = download_speeds[0]
                mirror_stats[mirror] = result_speed

                mytxt = "%s: %s, %s/sec" % (
                    blue(_("Mirror speed")),
                    purple(hostname),
                    teal(str(entropy.tools.bytes_into_human(result_speed))),
                )
                self.output(
                    mytxt,
                    importance = 1,
                    level = "info",
                    header = brown(" @@ ")
                )
            finally:
                os.close(tmp_fd)
                os.remove(tmp_path)

        # calculate new order
        new_pkg_mirrors = sorted(mirror_stats.keys(),
            key = lambda x: mirror_stats[x])
        repo_data['plain_packages'] = new_pkg_mirrors
        removed = self.remove_repository(repository_id)
        if not removed:
            raise KeyError("cannot touch given repository (1)")
        added = self.add_repository(repo_data)
        if not added:
            raise KeyError("cannot touch given repository (2)")
        return repo_data

    def set_branch(self, branch):
        """
        Set new Entropy branch. This is NOT thread-safe.
        Please note that if you call this method all your
        repository instance references will become invalid.
        This is caused by close_repositories and SystemSettings
        clear methods.
        Once you changed branch, the repository databases won't be
        available until you fetch them (through Repositories class)

        @param branch -- new branch
        @type branch basestring
        @return None
        """
        cacher_started = self._cacher.is_started()
        self._cacher.discard()
        if cacher_started:
            self._cacher.stop()
        self.clear_cache()
        self.close_repositories()
        # etpConst should be readonly but we override the rule here
        # this is also useful when no config file or parameter into it exists
        etpConst['branch'] = branch
        repo_conf = self._settings.get_setting_files_data()['repositories']
        entropy.tools.write_parameter_to_file(repo_conf,
            "branch", branch)
        # there are no valid repos atm
        del self._enabled_repos[:]
        self._settings.clear()

        # reset treeupdatesactions
        self.reopen_installed_repository()
        self._installed_repository.resetTreeupdatesDigests()
        self._validate_repositories(quiet = True)
        self.close_repositories()
        if cacher_started:
            self._cacher.start()

    def get_meant_packages(self, search_term, from_installed = False,
        valid_repos = None):
        """
        Return a list of package matches that are phonetically similar to
        search_term string.

        @param search_string: the search string
        @type search_string: string
        @keyword from_installed: if packages should be searched inside the
            installed packages repository only (instead of available
            package repositories, the default)
        @type from_installed: bool
        @keyword valid_repos: list of repository identifiers that should
            be used instead of default ones.
        @type valid_repos: list
        @return: list of package matches
        @rtype: list
        """
        if valid_repos is None:
            valid_repos = []

        pkg_data = []
        atom_srch = False
        if "/" in search_term:
            atom_srch = True

        if from_installed:
            if hasattr(self, '_installed_repository'):
                if self._installed_repository is not None:
                    valid_repos.append(self._installed_repository)

        elif not valid_repos:
            valid_repos.extend(self._filter_available_repositories())

        for repo in valid_repos:
            if const_isstring(repo):
                dbconn = self.open_repository(repo)
            elif isinstance(repo, EntropyRepositoryBase):
                dbconn = repo
            else:
                continue
            pkg_data.extend([(x, repo,) for x in \
                dbconn.searchSimilarPackages(search_term, atom = atom_srch)])

        return pkg_data

    def get_package_groups(self):
        """
        Return Entropy Package Groups metadata. The returned dictionary
        contains information to make Entropy Client users to group packages
        into "macro" categories.

        @return: Entropy Package Groups metadata
        @rtype: dict
        """
        spm = self.Spm_class()
        groups = spm.get_package_groups().copy()

        # expand metadata
        categories = self._get_package_categories()
        for data in list(groups.values()):

            exp_cats = set()
            for g_cat in data['categories']:
                exp_cats.update([x for x in categories if x.startswith(g_cat)])
            data['categories'] = sorted(exp_cats)

        return groups

    def _get_package_categories(self):
        categories = set()
        for repo in self._enabled_repos:
            dbconn = self.open_repository(repo)
            try:
                categories.update(dbconn.listAllCategories())
            except EntropyRepositoryError:
                # on broken repos this might cause issues
                continue
        return sorted(categories)

    def _inject_entropy_database_into_package(self, package_filename, data,
        treeupdates_actions = None, initialized_repository_path = None):

        already_initialized = False
        tmp_fd = None
        if initialized_repository_path is not None:
            tmp_path = initialized_repository_path
            already_initialized = True
        else:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="entropy.client.methods._inject_edb")

        try:
            dbconn = self.open_generic_repository(tmp_path)
            if not already_initialized:
                dbconn.initializeRepository()
            dbconn.addPackage(data, revision = data['revision'])
            if treeupdates_actions is not None:
                dbconn.bumpTreeUpdatesActions(treeupdates_actions)
            dbconn.commit()
            dbconn.close()
            entropy.tools.aggregate_entropy_metadata(package_filename, tmp_path)
        finally:
            if not already_initialized:
                os.close(tmp_fd)
                os.remove(tmp_path)

    def generate_package(self, entropy_package_metadata, save_directory,
        edb = True, fake = False, compression = "bz2", shiftpath = None):
        """
        Generate a valid Entropy package file from a full package metadata
        object (see entropy.db.skel.EntropyRepositoryBase.getPackageData()) and
        save it into save_directory directory, package content is read from
        disk, so this method works fine ONLY for installed packages.

        @param entropy_package_metadata: entropy package metadata
        @type entropy_package_metadata: dict
        @param save_directory: directory where to store the package file
        @type save_directory: string
        @keyword edb: add Entropy database metadata at the end of the file
        @type edb: bool
        @keyword fake: create a fake package (empty)
        @type fake: bool
        @keyword compression: supported compressions: "gz", "bz2" or "" (no
            compression)
        @type compression: string
        @keyword shiftpath: if package files are stored into an alternative
            root directory.
        @type shiftpath: string
        @return: path to generated package file or None (if error)
        @rtype: string or None
        """
        import tarfile
        if compression not in ("bz2", "", "gz"):
            compression = "bz2"
        if shiftpath is None:
            shiftpath = os.path.sep
        elif not shiftpath:
            shiftpath = os.path.sep

        version = entropy_package_metadata['version']
        version += "%s%s" % (etpConst['entropyrevisionprefix'],
            entropy_package_metadata['revision'],)
        pkgname = entropy.dep.create_package_filename(
            entropy_package_metadata['category'],
            entropy_package_metadata['name'], version,
            entropy_package_metadata['versiontag'])

        pkg_path = os.path.join(save_directory, pkgname)
        if os.path.isfile(pkg_path):
            os.remove(pkg_path)

        tar = tarfile.open(pkg_path, "w:"+compression)

        if not fake:

            contents = sorted(entropy_package_metadata['content'])

            # collect files
            for orig_path in contents:
                # convert back to filesystem str
                encoded_path = orig_path
                orig_path = const_convert_to_rawstring(orig_path)
                strip_orig_path = orig_path.lstrip(os.path.sep)
                path = os.path.join(shiftpath, strip_orig_path)
                try:
                    exist = os.lstat(path)
                except OSError:
                    continue # skip file
                ftype = entropy_package_metadata['content'][encoded_path]
                if str(ftype) == '0':
                    # force match below, '0' means databases without ftype
                    ftype = 'dir'
                if 'dir' == ftype and \
                    not stat.S_ISDIR(exist.st_mode) and \
                    os.path.isdir(path):
                    # workaround for directory symlink issues
                    path = os.path.realpath(path)

                tarinfo = tar.gettarinfo(path, strip_orig_path)

                if stat.S_ISREG(exist.st_mode):
                    with open(path, "rb") as f:
                        tar.addfile(tarinfo, f)
                else:
                    tar.addfile(tarinfo)

        tar.close()

        # append SPM metadata
        spm = self.Spm()
        pkgatom = entropy.dep.create_package_atom_string(
            entropy_package_metadata['category'],
            entropy_package_metadata['name'],
            entropy_package_metadata['version'],
            entropy_package_metadata['versiontag'])
        spm.append_metadata_to_package(pkgatom, pkg_path)
        if edb:
            self._inject_entropy_database_into_package(pkg_path,
                entropy_package_metadata)

        if os.path.isfile(pkg_path):
            return pkg_path
        return None


class MatchMixin:

    def get_package_action(self, package_match):
        """
        For given package match, return an action value representing the
        current status of the package: either "upgradable" (return status: 2),
        "installable" (return status: 1), "reinstallable" (return status: 0),
        "downgradable" (return status -1).

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @return: package status
        @rtype: int
        """
        pkg_id, pkg_repo = package_match
        dbconn = self.open_repository(pkg_repo)
        pkgkey, pkgslot = dbconn.retrieveKeySlot(pkg_id)
        results = self._installed_repository.searchKeySlot(pkgkey, pkgslot)
        if not results:
            return 1

        installed_idpackage = sorted(results)[-1]
        pkgver, pkgtag, pkgrev = dbconn.getVersioningData(pkg_id)
        ver_data = \
            self._installed_repository.getVersioningData(installed_idpackage)
        if ver_data is None:
            # installed idpackage is not available, race condition, probably
            return 1
        installed_ver, installed_tag, installed_rev = ver_data
        pkgcmp = entropy.dep.entropy_compare_versions(
            (pkgver, pkgtag, pkgrev),
            (installed_ver, installed_tag, installed_rev))
        if pkgcmp == 0:
            # check digest, if it differs, we should mark pkg as update
            # we don't want users to think that they are "reinstalling" stuff
            # because it will just confuse them
            inst_digest = self._installed_repository.retrieveDigest(
                installed_idpackage)
            repo_digest = dbconn.retrieveDigest(pkg_id)
            if inst_digest != repo_digest:
                return 2
            return 0
        elif pkgcmp > 0:
            return 2
        return -1

    def is_package_masked(self, package_match, live_check = True):
        """
        Determine whether given package match belongs to a masked package.
        If live_check is True, even temporary masks (called live masking because
        they belong to this running Entropy instance only) will be considered.

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @keyword live_check: check for live masks (default is True)
        @type live_check: bool
        @return: True, if package is masked, False otherwise
        @rtype: bool
        """
        m_id, m_repo = package_match
        dbconn = self.open_repository(m_repo)
        idpackage, idreason = dbconn.maskFilter(m_id, live = live_check)
        if idpackage != -1:
            return False
        return True

    def is_package_masked_by_user(self, package_match, live_check = True):
        """
        Determine whether given package match belongs to a masked package,
        requested by user (user explicitly masked the package).
        If live_check is True, even temporary masks (called live masking because
        they belong to this running Entropy instance only) will be considered.

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @keyword live_check: check for live masks (default is True)
        @type live_check: bool
        @return: True, if package is masked by user, False otherwise
        @rtype: bool
        """
        m_id, m_repo = package_match
        if m_repo not in self._enabled_repos:
            return False
        dbconn = self.open_repository(m_repo)
        idpackage, idreason = dbconn.maskFilter(m_id, live = live_check)
        if idpackage != -1:
            return False

        myr = self._settings['pkg_masking_reference']
        user_masks = [myr['user_package_mask'], myr['user_license_mask'],
            myr['user_live_mask']]
        if idreason in user_masks:
            return True
        return False

    def is_package_unmasked_by_user(self, package_match, live_check = True):
        """
        Determine whether given package match belongs to an unmasked package,
        requested by user (user explicitly unmasked the package).
        If live_check is True, even temporary masks (called live masking because
        they belong to this running Entropy instance only) will be considered.

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @keyword live_check: check for live masks (default is True)
        @type live_check: bool
        @return: True, if package is unmasked by user, False otherwise
        @rtype: bool
        """
        m_id, m_repo = package_match
        if m_repo not in self._enabled_repos:
            return False
        dbconn = self.open_repository(m_repo)
        idpackage, idreason = dbconn.maskFilter(m_id, live = live_check)
        if idpackage == -1:
            return False

        myr = self._settings['pkg_masking_reference']
        user_masks = [
            myr['user_package_unmask'], myr['user_live_unmask'],
            myr['user_package_keywords'], myr['user_repo_package_keywords_all'],
            myr['user_repo_package_keywords']
        ]
        if idreason in user_masks:
            return True
        return False

    def mask_package(self, package_match, method = 'atom', dry_run = False):
        """
        Mask given package match. Two masking methods are available: either by
        "atom" (exact package string will be used) or by "keyslot" (package
        key + slot combo will be used).

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @keyword method: masking method (either "atom" or "keyslot").
        @type method: string
        @keyword dry_run: execute a "dry" run
        @type dry_run: bool
        @return: True, if package has been masked successfully
        @rtype: bool
        """
        if self.is_package_masked(package_match, live_check = False):
            return True
        methods = {
            'atom': self._mask_package_by_atom,
            'keyslot': self._mask_package_by_keyslot,
        }
        rc = self._mask_unmask_package(package_match, method, methods,
            dry_run = dry_run)
        if dry_run: # inject if done "live"
            lpm = self._settings['live_packagemasking']
            lpm['unmask_matches'].discard(package_match)
            lpm['mask_matches'].add(package_match)
        return rc

    def unmask_package(self, package_match, method = 'atom', dry_run = False):
        """
        Unmask given package match. Two unmasking methods are available: either
        by "atom" (exact package string will be used) or by "keyslot" (package
        key + slot combo will be used).

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @keyword method: masking method (either "atom" or "keyslot").
        @type method: string
        @keyword dry_run: execute a "dry" run
        @type dry_run: bool
        @return: True, if package has been unmasked successfully
        @rtype: bool
        """
        if not self.is_package_masked(package_match, live_check = False):
            return True
        methods = {
            'atom': self._unmask_package_by_atom,
            'keyslot': self._unmask_package_by_keyslot,
        }
        rc = self._mask_unmask_package(package_match, method, methods,
            dry_run = dry_run)
        if dry_run: # inject if done "live"
            lpm = self._settings['live_packagemasking']
            lpm['unmask_matches'].add(package_match)
            lpm['mask_matches'].discard(package_match)
        return rc

    def _mask_unmask_package(self, package_match, method, methods_reference,
        dry_run = False):

        f = methods_reference.get(method)
        if not hasattr(f, '__call__'):
            raise AttributeError('%s: %s' % (
                _("not a valid method"), method,) )

        self._cacher.discard()
        self._settings._clear_repository_cache(package_match[1])
        done = f(package_match, dry_run)
        if done and not dry_run:
            self._settings.clear()

        cl_id = self.sys_settings_client_plugin_id
        self._settings[cl_id]['masking_validation']['cache'].clear()
        return done

    def _unmask_package_by_atom(self, package_match, dry_run = False):
        m_id, m_repo = package_match
        dbconn = self.open_repository(m_repo)
        atom = dbconn.retrieveAtom(m_id)
        return self.unmask_package_generic(package_match, atom,
            dry_run = dry_run)

    def _unmask_package_by_keyslot(self, package_match, dry_run = False):
        m_id, m_repo = package_match
        dbconn = self.open_repository(m_repo)
        key, slot = dbconn.retrieveKeySlot(m_id)
        keyslot = "%s%s%s" % (key, etpConst['entropyslotprefix'], slot,)
        return self.unmask_package_generic(package_match, keyslot,
            dry_run = dry_run)

    def _mask_package_by_atom(self, package_match, dry_run = False):
        m_id, m_repo = package_match
        dbconn = self.open_repository(m_repo)
        atom = dbconn.retrieveAtom(m_id)
        return self.mask_package_generic(package_match, atom, dry_run = dry_run)

    def _mask_package_by_keyslot(self, package_match, dry_run = False):
        m_id, m_repo = package_match
        dbconn = self.open_repository(m_repo)
        key, slot = dbconn.retrieveKeySlot(m_id)
        keyslot = "%s%s%s" % (key, etpConst['entropyslotprefix'], slot)
        return self.mask_package_generic(package_match, keyslot,
            dry_run = dry_run)

    def unmask_package_generic(self, package_match, keyword, dry_run = False):
        """
        Unmask package using string passed in "keyword". A package match is
        still required because previous masks have to be cleared.

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @param keyword: the package string to unmask
        @type keyword: string
        @keyword dry_run: execute a "dry" run
        @type dry_run: bool
        @return: True, if unmask went fine, False otherwise
        @rtype: bool
        """
        self._clear_package_mask(package_match, dry_run)
        m_file = self._settings.get_setting_files_data()['unmask']
        return self._mask_unmask_package_generic(keyword, m_file,
            dry_run = dry_run)

    def mask_package_generic(self, package_match, keyword, dry_run = False):
        """
        Mask package using string passed in "keyword". A package match is
        still required because previous unmasks have to be cleared.

        @param package_match: entropy package match (package_id, repository_id)
        @type package_match: tuple
        @param keyword: the package string to mask
        @type keyword: string
        @keyword dry_run: execute a "dry" run
        @type dry_run: bool
        @return: True, if mask went fine, False otherwise
        @rtype: bool
        """
        self._clear_package_mask(package_match, dry_run)
        m_file = self._settings.get_setting_files_data()['mask']
        return self._mask_unmask_package_generic(keyword, m_file,
            dry_run = dry_run)

    def _mask_unmask_package_generic(self, keyword, m_file, dry_run = False):
        exist = False
        if not os.path.isfile(m_file):
            if not os.access(os.path.dirname(m_file), os.W_OK):
                return False # cannot write
        elif not os.access(m_file, os.W_OK):
            return False
        elif not dry_run:
            exist = True

        if dry_run:
            return True

        content = []
        enc = etpConst['conf_encoding']
        if exist:
            with codecs.open(m_file, "r", encoding=enc) as f:
                content = [x.strip() for x in f.readlines()]
        content.append(keyword)

        entropy.tools.atomic_write(m_file, "\n".join(content) + "\n", enc)
        return True

    def _clear_package_mask(self, package_match, dry_run = False):
        setting_data = self._settings.get_setting_files_data()
        masking_list = [setting_data['mask'], setting_data['unmask']]

        setting_dirs = self._settings.get_setting_dirs_data()
        conf_dir, conf_files, skipped_files, auto_upd = setting_dirs['mask_d']
        masking_list += [conf_p for conf_p, mtime_p in conf_files]
        conf_dir, conf_files, skipped_files, auto_upd = setting_dirs['unmask_d']
        masking_list += [conf_p for conf_p, mtime_p in conf_files]
        return self._clear_match_generic(package_match,
            masking_list = masking_list, dry_run = dry_run)

    def _clear_match_generic(self, match, masking_list = None, dry_run = False):

        if dry_run:
            return

        if masking_list is None:
            masking_list = []

        self._settings['live_packagemasking']['unmask_matches'].discard(
            match)
        self._settings['live_packagemasking']['mask_matches'].discard(
            match)

        new_mask_list = [x for x in masking_list if os.path.isfile(x) \
            and os.access(x, os.W_OK)]

        enc = etpConst['conf_encoding']
        for mask_file in new_mask_list:

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="entropy.client.methods._clear_match_gen")

            with codecs.open(mask_file, "r", encoding=enc) as mask_f:
                with os.fdopen(tmp_fd, "w") as tmp_f:
                    for line in mask_f.readlines():
                        strip_line = line.strip()

                        if not (strip_line.startswith("#") or \
                                    not strip_line):
                            mymatch = self.atom_match(strip_line,
                                mask_filter = False)
                            if mymatch == match:
                                continue

                        tmp_f.write(line)

            entropy.tools.rename_keep_permissions(
                tmp_path, mask_file)

    def search_installed_mimetype(self, mimetype):
        """
        Given a mimetype, return list of installed package identifiers
        belonging to packages that can handle it.

        @param mimetype: mimetype string
        @type mimetype: string
        @return: list of installed package identifiers
        @rtype: list
        """
        return self._installed_repository.searchProvidedMime(mimetype)

    def search_available_mimetype(self, mimetype):
        """
        Given a mimetype, return list of available package matches
        belonging to packages that can handle it.

        @param mimetype: mimetype string
        @type mimetype: string
        @return: list of available package matches
        @rtype: list
        """
        packages = []
        for repo in self._enabled_repos:
            repo_db = self.open_repository(repo)
            packages += [(x, repo) for x in \
                repo_db.searchProvidedMime(mimetype)]
        return packages
