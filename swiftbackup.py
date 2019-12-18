#!/usr/bin/env python3
import configparser
import errno
import fcntl
import getopt
import itertools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict, namedtuple
from contextlib import contextmanager
from functools import partial

DEFAULT_CONFIG_FILE = os.getenv('SWIFTBACKUP_CONFIG', '/etc/swiftbackup.conf')

RSYNC_SUCCESS = (0, 24)

USAGE = '''Usage:

    swiftbackup [--help] [--version] [--write-config]
    swiftbackup [options] sync [--rotate] <target>...
    swiftbackup [options] rotate <target>...
    swiftbackup [options] status [<target>...]
'''

HELP = '''swiftbackup

%(USAGE)s

Options:
    -h, --help           Show this help and exit.
    --version            Print version number and exit.
    -w, --write-config   Write configuration defaults to stdout.
    -c, --config=<file>  Read configuration from <file>
                         (default: %(DEFAULT_CONFIG_FILE)s).
    -r, --rotate         Rotate snapshots after sync.
    -f, --force          With 'sync', make a new snapshot even when one
                         already exists for the current time interval.
    -n, --dry-run        Print what would be done but do not transfer data or
                         rotate snapshots.
    --utc                Use UTC instead of local time.
    --debug              Print debug info about which external commands are
                         being executed to stderr.
''' % {'USAGE': USAGE, 'DEFAULT_CONFIG_FILE': DEFAULT_CONFIG_FILE}

__doc__ = HELP

__version__ = '1.0'

CONFIG_DEFAULTS = {
    'retain_hourly': '6',
    'retain_daily': '7',
    'retain_weekly': '4',
    'retain_monthly': '6',
    'retain_yearly': '0',
    'rsync_bin': shutil.which('rsync') or '/usr/bin/rsync',
    'rsync_defaults': '-ax --delete --delete-excluded --numeric-ids --relative',
    'rsync_options': '',
    'rsync_exclude': '',
    'ping_cmd': '%s -w1 -c1' % (shutil.which('ping') or '/bin/ping'),
    'link_fmt': '%Y-%m-%d.%H%M',
    'backup_directory': '/srv/swiftbackup',
}

CONFIG_TEMPLATE = '''[defaults]
retain_hourly = %(retain_hourly)s
retain_daily = %(retain_daily)s
retain_weekly = %(retain_weekly)s
retain_monthly = %(retain_monthly)s
retain_yearly = %(retain_yearly)s
rsync_bin = %(rsync_bin)s
rsync_defaults = %(rsync_defaults)s
rsync_options = %(rsync_options)s
rsync_exclude = %(rsync_exclude)s
ping_cmd = %(ping_cmd)s
link_fmt = %(link_fmt)s
backup_directory = %(backup_directory)s
'''

Target = namedtuple('target', 'name config backups ping')

Config = namedtuple('Config', '''
    retain_hourly
    retain_daily
    retain_weekly
    retain_monthly
    retain_yearly
    rsync_bin
    rsync_defaults
    rsync_options
    rsync_exclude
    ping_cmd
    link_fmt
    backup_directory
''')

Backup = namedtuple('Backup', 'src dst rsync_options')

Options = namedtuple('Options', '''
    help
    version
    write_config
    config
    rotate
    force
    dry_run
    padding
''')

class Snapshot(namedtuple('Snapshot', 'timestamp dirname path')):
    @classmethod
    def from_path(cls, path):
        path = os.path.abspath(path)
        dirname = os.path.basename(path)
        return cls(
            timestamp=int(dirname),
            dirname=dirname,
            path=path,
        )

DEBUG = False
USE_UTC = False

DEFAULT_OPTIONS = Options(
    help=False,
    version=False,
    write_config=False,
    config=DEFAULT_CONFIG_FILE,
    rotate=False,
    force=False,
    dry_run=False,
    padding=0,
)

class ConfigError(RuntimeError): pass
class CommandError(RuntimeError): pass
class LockfileError(RuntimeError): pass


def to_timetuple(ts):
    return time.gmtime(ts) if USE_UTC else time.localtime(ts)


class Commands:

    class chmod(namedtuple('chmod', 'path mode')):
        def run(self):
            return os.chmod(self.path, self.mode)

    class rename(namedtuple('chmod', 'src dst')):
        def run(self):
            return os.rename(self.src, self.dst)

    class touch(namedtuple('touch', 'path')):
        def run(self):
            return os.utime(self.path)

    class unlink(namedtuple('unlink', 'path')):
        def run(self):
            return os.unlink(self.path)

    class symlink(namedtuple('symlink', 'target link_name')):
        def run(self):
            return os.symlink(self.target, self.link_name)

    class rmdir(namedtuple('rmdir', 'path')):
        def run(self):
            return os.rmdir(self.path)

    class _mkdir(namedtuple('mkdir', 'path parents')):
        def run(self):
            if self.parents:
                os.makedirs(self.path, exist_ok=True)
            else:
                os.mkdir(self.path)

    class _mkdtemp(namedtuple('mkdtemp', 'prefix dir')):
        def run(self):
            return tempfile.mkdtemp(prefix=self.prefix, dir=self.dir)

    class _rmtree(namedtuple('rmtree', 'path ignore_errors')):
        def run(self):
            return shutil.rmtree(self.path, ignore_errors=self.ignore_errors)

    class _subprocess(namedtuple('subprocess', 'cmd rc_ok quiet')):
        def run(self):
            stdout = subprocess.DEVNULL if self.quiet else None
            debug('command=%s' % self.format_cmd(self.cmd))
            proc = subprocess.run(self.cmd, stdout=stdout)
            debug('  rc=%s, rc_ok=%s' % (proc.returncode, self.rc_ok))
            if proc.returncode not in self.rc_ok:
                raise CommandError('Command failed with status %d: %s'
                        % (proc.returncode, self.format_cmd(self.cmd)))
            return proc.returncode

        @staticmethod
        def format_cmd(cmd):
            return ' '.join([shlex.quote(s) for s in cmd])

    def mkdir(self, path, parents=False):
        return self._mkdir(path, parents)

    def mkdtemp(self, prefix=None, dir=None):
        return self._mkdtemp(prefix, dir)

    def rmtree(self, path, ignore_errors=False):
        return self._rmtree(path, ignore_errors)

    def subprocess(self, cmd, rc_ok=(0,), quiet=False):
        return self._subprocess(cmd, rc_ok, quiet)


CMD = Commands()


def log(*args):
    print(*args, flush=True)


def warn(*args):
    print(*args, file=sys.stderr, flush=True)


def debug(*args):
    if DEBUG:
        warn('DEBUG:', *args)


@contextmanager
def cd(directory):
    cwd = os.getcwd()
    os.chdir(directory)
    try:
        debug('cwd=%s' % directory)
        yield
    finally:
        os.chdir(cwd)
        debug('cwd=%s' % cwd)


@contextmanager
def lock(filename):
    with open(filename, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise LockfileError("Failed to acquire lock on %s"
                        % os.path.abspath(filename))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def parse_backup(line):
    parts = shlex.split(line)
    rsync_options = [s for s in parts if s.startswith('-')]
    rest = [s for s in parts if not s.startswith('-')]
    if not rest:
        raise ConfigError("source is required for backup")
    elif len(rest) == 1:
        src, dst = rest[0], ''
    elif len(rest) == 2:
        src, dst = rest
    else:
        raise ConfigError("unknown parameter(s) for backup: %s" % ' '.join(rest[2:]))
    dst, has_trailing_slash = os.path.normpath(dst), dst.endswith('/')
    if os.path.isabs(dst) or os.path.split(dst)[0] == os.pardir:
        raise ConfigError("destination must be a relative path inside the snapshot directory: %s" % dst)
    if dst == '.':
        dst = ''
    elif has_trailing_slash:
        dst += '/'
    return Backup(src=src, dst=dst, rsync_options=rsync_options)


def validate_option_names(section, mapping):
    if section == 'defaults':
        names = set(CONFIG_DEFAULTS)
    else:
        names = set(CONFIG_DEFAULTS) | {'backup', 'ping'}
    name = next((k for k in mapping if k not in names), None)
    if name:
        raise ConfigError("in section [%s]: unknown option '%s'" % (section, name))


def read_config(filename, content=None):
    cfg = configparser.ConfigParser(
        defaults=CONFIG_DEFAULTS,
        default_section='defaults',
        interpolation=None,
    )
    if content is not None:
        cfg.read_string(content, source=filename)
    else:
        if not cfg.read(filename):  # pragma: no branch
            raise ConfigError('file does not exist or is not readable')

    validate_option_names('defaults', cfg.defaults())

    targets = OrderedDict()
    for key in cfg.sections():
        name = key.strip()
        if os.path.split(name)[0] != '':
            raise ConfigError("target name cannot contain path separator: %s" % name)
        elif name == 'all':
            raise ConfigError("not a valid target name: %s" % name)
        section = cfg[key]
        validate_option_names(key, section)
        try:
            backups = [
                parse_backup(line)
                for line in section['backup'].splitlines()
                if line and not line.isspace()
            ]
            config = Config(
                retain_hourly=abs(section.getint('retain_hourly')),
                retain_daily=abs(section.getint('retain_daily')),
                retain_weekly=abs(section.getint('retain_weekly')),
                retain_monthly=abs(section.getint('retain_monthly')),
                retain_yearly=abs(section.getint('retain_yearly')),
                rsync_bin=section['rsync_bin'],
                rsync_defaults=shlex.split(section['rsync_defaults']),
                rsync_options=shlex.split(section['rsync_options']),
                rsync_exclude=shlex.split(section['rsync_exclude']),
                ping_cmd=shlex.split(section['ping_cmd']),
                link_fmt=section['link_fmt'],
                backup_directory=section['backup_directory'],
            )
            targets[name] = Target(
                name=name,
                config=config,
                backups=backups,
                ping=shlex.split(section.get('ping', '')),
            )
        except KeyError as e:
            raise ConfigError("in section [%s]: missing option %s" % (key, e))
        except ConfigError as e:
            raise ConfigError("in section [%s]: %s" % (key, e))

    return targets


def unique(iterable, key, first=False):
    for _, values in itertools.groupby(iterable, key):
        yield list(values)[0] if first else list(values)[-1]


hour  = lambda ts: time.strftime('%Y-%m-%d %H', to_timetuple(ts))
day   = lambda ts: time.strftime('%Y-%m-%d',    to_timetuple(ts))
week  = lambda ts: time.strftime('%Y.%W',       to_timetuple(ts))
month = lambda ts: time.strftime('%Y-%m',       to_timetuple(ts))
year  = lambda ts: time.strftime('%Y',          to_timetuple(ts))

hourly  = partial(unique, key=lambda s: hour(s.timestamp))
daily   = partial(unique, key=lambda s: day(s.timestamp))
weekly  = partial(unique, key=lambda s: week(s.timestamp))
monthly = partial(unique, key=lambda s: month(s.timestamp))
yearly  = partial(unique, key=lambda s: year(s.timestamp))


def take(n, iterable):
    return list(itertools.islice(iterable, n))


def read_snapshots(directory):
    if not os.path.exists(directory):
        return []
    snapshots = []
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if not os.path.islink(path) and entry.isdigit():
            snapshots.append(Snapshot.from_path(path))
    return snapshots


def rotate(cfg, snapshots):
    snapshots = sorted(snapshots, reverse=True)
    keep = [
        take(cfg.retain_hourly,  hourly(snapshots, first=True)),
        take(cfg.retain_daily,   daily(snapshots)),
        take(cfg.retain_weekly,  weekly(snapshots)),
        take(cfg.retain_monthly, monthly(snapshots)),
        take(cfg.retain_yearly,  yearly(snapshots)),
    ]
    remove = set(snapshots) - set(sum(keep, []))
    return keep, sorted(remove, reverse=True)


def gen_backup_cmds(options, target, backup, directory, link_dest=None):
    cfg = target.config
    dst = os.path.join(directory, backup.dst)
    rsync_cmd = [cfg.rsync_bin]
    if options.dry_run:
        rsync_cmd += ['--dry-run']
    rsync_cmd += cfg.rsync_defaults + cfg.rsync_options + backup.rsync_options
    for exclude in cfg.rsync_exclude:
        rsync_cmd += ['--exclude', exclude]
    if link_dest:
        rsync_cmd += ['--link-dest', link_dest]
    rsync_cmd += [backup.src, dst]
    yield CMD.mkdir(dst, parents=True)
    yield CMD.subprocess(rsync_cmd, rc_ok=RSYNC_SUCCESS)


def gen_sync_cmds(options, target, timestamp, link_dest=None):
    yield CMD.mkdir('snapshots', parents=True)
    for host in target.ping:
        result = yield CMD.subprocess(target.config.ping_cmd + [host], quiet=True)
        if result == 1:
            log("Host %s is not up, skipping sync for target %s" % (host, target.name))
            return

    log("Creating new snapshot for target %s at %s" % (
        target.name,
        time.strftime('%Y-%m-%d %H:%M', to_timetuple(timestamp))
    ))
    tmpdir = yield CMD.mkdtemp(prefix='rsync.', dir='snapshots')
    try:
        for backup in target.backups:
            yield from gen_backup_cmds(options, target, backup, tmpdir, link_dest)
    except CommandError as e:
        # If a command fails, clean up our tmpdir and re-raise
        yield CMD.rmtree(tmpdir, ignore_errors=True)
        raise e

    if options.dry_run:
        yield CMD.rmdir(tmpdir)
    else:
        yield CMD.touch(tmpdir)
        yield CMD.chmod(tmpdir, 0o755)
        yield CMD.rename(tmpdir, os.path.join('snapshots', str(int(timestamp))))


def gen_rotate_cmds(options, target, snapshots):
    paths = []
    _, remove = rotate(target.config, snapshots)
    for snapshot in remove:
        dirname = os.path.join('snapshots', snapshot.dirname)
        log("Removing snapshot for target %s from %s" % (
            target.name,
            time.strftime('%Y-%m-%d %H:%M', to_timetuple(snapshot.timestamp))
        ))
        if not options.dry_run:
            renamed = dirname + '.remove'
            yield CMD.rename(dirname, renamed)
            paths.append(renamed)
    return paths


def gen_update_symlink_cmds(target, links, snapshots):
    for link in links:
        yield CMD.unlink(link)
    link_fmt = target.config.link_fmt
    link_names = {
        time.strftime(link_fmt, to_timetuple(s.timestamp)): s
        for s in sorted(snapshots)
    }
    for link_name, snapshot in sorted(link_names.items()):
        target = os.path.join('snapshots', snapshot.dirname)
        yield CMD.symlink(target, link_name)


def run_commands(gen):
    try:
        cmd = next(gen)
    except StopIteration as e:
        return e.value

    while True:
        try:
            try:
                result = cmd.run()
                cmd = gen.send(result)
            except CommandError as e:
                # Re-raise the exception inside the generator to give it a
                # chance to clean up.
                cmd = gen.throw(e)
        except StopIteration as e:
            return e.value


def update_symlinks(target):
    links = [
        entry for entry in os.listdir()
        if os.path.islink(entry) and not entry.startswith('.')
    ]
    snapshots = read_snapshots('snapshots')
    cmd_gen = gen_update_symlink_cmds(target, links, snapshots)
    run_commands(cmd_gen)


def action_sync(options, target):
    cfg = target.config
    timestamp = time.time()
    with lock('.lock'):
        link_dest = None
        snapshots = read_snapshots('snapshots')
        keep, _ = rotate(cfg, snapshots)
        hourly, daily, weekly, monthly, yearly = keep
        if not options.force:
            if cfg.retain_hourly:
                if any(hour(s.timestamp) == hour(timestamp) for s in hourly):
                    return
            elif cfg.retain_daily:
                if any(day(s.timestamp) == day(timestamp) for s in daily):
                    return
            elif cfg.retain_weekly:
                if any(week(s.timestamp) == week(timestamp) for s in weekly):
                    return
            elif cfg.retain_monthly:
                if any(month(s.timestamp) == month(timestamp) for s in monthly):
                    return
            elif cfg.retain_yearly:
                if any(year(s.timestamp) == year(timestamp) for s in yearly):
                    return
            else:
                return

        if snapshots:
            link_dest = sorted(snapshots, reverse=True)[0].path
        cmd_gen = gen_sync_cmds(options, target, timestamp, link_dest)
        run_commands(cmd_gen)
        if not options.dry_run:
            update_symlinks(target)

    if options.rotate:
        action_rotate(options, target)


def action_rotate(options, target):
    with lock('.lock'):
        snapshots = read_snapshots('snapshots')
        cmd_gen = gen_rotate_cmds(options, target, snapshots)
        paths = run_commands(cmd_gen)
        if not options.dry_run:
            update_symlinks(target)

    for path in paths:
        CMD.subprocess(['rm', '-rf', path]).run()


def action_status(options, target):
    snapshots = read_snapshots('snapshots')
    keep, _ = rotate(target.config, snapshots)
    hourly, daily, weekly, monthly, yearly = [set(l) for l in keep]
    for snapshot in sorted(snapshots, reverse=True):
        print(target.name.ljust(options.padding), end='  ')
        print(time.strftime(
            '%Y-%m-%d %H:%M   %W', to_timetuple(snapshot.timestamp)
        ), end='  ')
        print('h' if snapshot in hourly  else ' ', end=' ')
        print('d' if snapshot in daily   else ' ', end=' ')
        print('w' if snapshot in weekly  else ' ', end=' ')
        print('m' if snapshot in monthly else ' ', end=' ')
        print('y' if snapshot in yearly  else ' ', end=' ')
        print()


def print_status_header(options, targets):
    padding = max(max(len(t.name) for t in targets), 6)
    print('%s  Date       Time  Week  Snapshot ' % 'Target'.ljust(padding))
    print('%s==================================' % ''.ljust(padding, '='))
    return options._replace(padding=padding)


def run_action(action, options, targets):
    if action is action_status:
        options = print_status_header(options, targets)

    failed_targets = []
    for target in targets:
        try:
            cfg = target.config
            if not os.path.exists(cfg.backup_directory):
                raise RuntimeError('[%s] backup directory does not exist: %s'
                        % (target.name, cfg.backup_directory))
            directory = os.path.join(cfg.backup_directory, target.name)
            os.makedirs(directory, 0o755, exist_ok=True)
            with cd(directory):
                action(options, target)
        except RuntimeError as e:
            warn("[%s] Error: %s" % (target.name, e))
            failed_targets.append(target)

    if failed_targets:
        warn("The following targets had errors:", ','.join(t.name for t in failed_targets))
        return 3
    else:
        return 0


def main():
    options = DEFAULT_OPTIONS
    try:
        opts, args = getopt.gnu_getopt(
            sys.argv[1:],
            'hwc:rfn',
            ['help', 'version', 'write-config', 'config', 'rotate', 'force',
                'dry-run', 'utc', 'debug'],
        )
    except getopt.GetoptError as e:
        warn(e)
        warn(USAGE)
        return 1

    for option, value in opts:
        if option in ('-h', '--help'):
            options = options._replace(help=True)
        elif option == '--version':
            options = options._replace(version=True)
        elif option in ('-w', '--write-config'):
            options = options._replace(write_config=True)
        elif option in ('-c', '--config'):
            options = options._replace(config=value)
        elif option in ('-r', '--rotate'):
            options = options._replace(rotate=True)
        elif option in ('-f', '--force'):
            options = options._replace(force=True)
        elif option in ('-n', '--dry-run'):
            options = options._replace(dry_run=True)
        elif option == '--utc':
            global USE_UTC
            USE_UTC = True
        elif option == '--debug':
            global DEBUG
            DEBUG = True
        else:
            raise RuntimeError("Unhandled option: %s %s" % (option, value))

    if not args:
        if options.help:
            print(HELP)
            return 0
        elif options.version:
            print('swiftbackup %s' % __version__)
            return 0
        elif options.write_config:
            print(CONFIG_TEMPLATE % CONFIG_DEFAULTS)
            return 0
        else:
            warn(USAGE)
            return 1

    action, args = args[0], args[1:]
    if options.help:
        warn("option --help not recognized for action %s" % action)
        warn(USAGE)
        return 1
    elif options.version:
        warn("option --version not recognized for action %s" % action)
        warn(USAGE)
        return 1
    elif options.write_config:
        warn("option --write-config not recognized for action %s" % action)
        warn(USAGE)
        return 1

    actions = {
        'sync': action_sync,
        'rotate': action_rotate,
        'status': action_status,
    }

    if action not in actions:
        warn("action '%s' not recognized" % action)
        warn(USAGE)
        return 1

    try:
        config = read_config(options.config)
    except ConfigError as e:
        warn('%s: %s' % (options.config, e))
        return 2

    targets = []
    for name in args:
        if name == 'all':
            targets = sorted(config.values(), key=lambda t: t.name)
        elif name not in config:
            warn('unknown target: %s' % name)
            warn('avaliable targets: %s' % ', '.join(sorted(config)))
            return 1
        else:
            target = config[name]
            if target not in targets:
                targets.append(target)

    if not targets:
        if action == 'status':
            targets = sorted(config.values(), key=lambda t: t.name)
        else:
            warn('target is required for action %s' % action)
            warn(USAGE)
            return 1

    return run_action(actions[action], options, targets)


if __name__ == '__main__':
    sys.exit(main())
