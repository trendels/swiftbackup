import shutil
from collections import OrderedDict
from datetime import datetime

import pytest
from pytest import raises as assert_raises

import swiftbackup as sw
from swiftbackup import CMD

sw.USE_UTC = True


default_config = sw.Config(
    retain_hourly=6,
    retain_daily=7,
    retain_weekly=4,
    retain_monthly=6,
    retain_yearly=0,
    rsync_bin=shutil.which('rsync') or '/usr/bin/rsync',
    rsync_defaults=['-ax', '--delete', '--delete-excluded', '--numeric-ids', '--relative'],
    rsync_options=[],
    rsync_exclude=[],
    ping_cmd=[(shutil.which('ping') or '/bin/ping'), '-w1', '-c1'],
    link_fmt='%Y-%m-%d.%H%M',
    backup_directory='/srv/swiftbackup',
)


@pytest.mark.parametrize('line, backup', [
    ('src', sw.Backup(src='src', dst='', rsync_options=[])),
    ('src dst', sw.Backup(src='src', dst='dst', rsync_options=[])),
    ('src -v dst', sw.Backup(src='src', dst='dst', rsync_options=['-v'])),
    ('--rsh=ssh x -v', sw.Backup(src='x', dst='', rsync_options=['--rsh=ssh', '-v'])),
    ('   x\ty', sw.Backup(src='x', dst='y', rsync_options=[])),
    ('a ./b', sw.Backup(src='a', dst='b', rsync_options=[])),
    ('a b/c', sw.Backup(src='a', dst='b/c', rsync_options=[])),
    ('a b/../c', sw.Backup(src='a', dst='c', rsync_options=[])),
    ('a b/', sw.Backup(src='a', dst='b/', rsync_options=[])),
])
def test_parse_backup(line, backup):
    assert sw.parse_backup(line) == backup


@pytest.mark.parametrize('line, msg_re', [
    ('', r'source is required'),
    ('-v', r'source is required'),
    ('a b c', r'unknown.*: c'),
    ('src /dst', r"destination must be a relative path"),
    ('src ../dst', r"destination must be .*inside the snapshot directory"),
    ('src a/../../b', r"destination must be .*inside the snapshot directory"),
])
def test_parse_backup_errors(line, msg_re):
    with assert_raises(sw.ConfigError, match=msg_re):
        sw.parse_backup(line)


def test_read_config():
    assert sw.read_config('test.config', content='') == OrderedDict()
    assert sw.read_config('test.config', content='[foo]\nbackup = user@host:/path') == OrderedDict([
        ('foo', sw.Target(
            name='foo',
            config=default_config,
            backups=[sw.Backup(src='user@host:/path', dst='', rsync_options=[])],
            ping=[],
        )),
    ])


def test_read_config_errors(tmp_path):
    filename = tmp_path / 'does-not-exist'
    with assert_raises(sw.ConfigError, match='file does not exist'):
        sw.read_config(filename)

    with assert_raises(sw.ConfigError, match=r'\[defaults\].*unknown option.*foo'):
        sw.read_config('test.config', content='[defaults]\nfoo=1')

    with assert_raises(sw.ConfigError, match=r'\[defaults\].*unknown option.*ping'):
        sw.read_config('test.config', content='[defaults]\nping=test')

    with assert_raises(sw.ConfigError, match=r'\[defaults\].*unknown option.*backup'):
        sw.read_config('test.config', content='[defaults]\nbackup=test')

    with assert_raises(sw.ConfigError, match=r'\[foo\].*unknown option.*foo'):
        sw.read_config('test.config', content='[foo]\nfoo=1')

    with assert_raises(sw.ConfigError, match=r'[foo].*missing option.*backup'):
        sw.read_config('test.config', content='[foo]\n')

    with assert_raises(sw.ConfigError, match=r'[foo].*backup'):
        x = sw.read_config('test.config', content='[foo]\nbackup=-v\n')

    with assert_raises(sw.ConfigError, match=r'not a valid target name.*all'):
        x = sw.read_config('test.config', content='[all]\nbackup=a b\n')

    with assert_raises(sw.ConfigError, match=r'cannot contain path separator: foo/bar'):
        x = sw.read_config('test.config', content='[foo/bar]\nbackup=a b\n')


def test_read_snapshots(tmp_path):
    for name in ('1234', '1235'):
        path = tmp_path / name
        path.mkdir()
    symlink = tmp_path / '9999'
    symlink.symlink_to(tmp_path / '1234')
    assert sw.read_snapshots(str(tmp_path / 'empty')) == []
    assert sw.read_snapshots(str(tmp_path)) == [
        sw.Snapshot(timestamp=1235, dirname='1235', path=str(tmp_path / '1235')),
        sw.Snapshot(timestamp=1234, dirname='1234', path=str(tmp_path / '1234')),
    ]


def cmdgen_test(gen, commands):
    if not commands:
        with assert_raises(StopIteration) as exc_info:
            result = next(gen)
        return exc_info.value.value

    cmd, last_result = commands[0]
    assert next(gen) == cmd
    for cmd, result in commands[1:]:
        if isinstance(last_result, Exception):
            assert gen.throw(last_result) == cmd
        else:
            assert gen.send(last_result) == cmd
        last_result = result

    with assert_raises(StopIteration) as exc_info:
        if isinstance(last_result, Exception):
            gen.throw(last_result)
        else:
            gen.send(last_result)
    return exc_info.value.value


@pytest.mark.parametrize('dry_run, commands, link_dest', [
    (False, [
        (CMD.mkdir('snapshots/.rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync',
            '-a', '-v', '-h', '--exclude', 'foo',
            'root@example.net:/etc', 'snapshots/.rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), 0),
    ], None),
    (True, [
        (CMD.mkdir('snapshots/.rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync',
            '--dry-run', '-a', '-v', '-h', '--exclude', 'foo',
            'root@example.net:/etc', 'snapshots/.rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), 0),
    ], None),
    (False, [
        (CMD.mkdir('snapshots/.rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync',
            '-a', '-v', '-h', '--exclude', 'foo', '--link-dest', 'snapshots/12345',
            'root@example.net:/etc', 'snapshots/.rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), 0),
    ], 'snapshots/12345'),
])
def test_gen_backup_cmds(dry_run, commands, link_dest):
    cfg = default_config._replace(
        rsync_bin='/usr/bin/rsync',
        rsync_defaults=['-a'],
        rsync_options=['-v'],
        rsync_exclude=['foo'],
    )
    options = sw.DEFAULT_OPTIONS._replace(dry_run=dry_run)
    backup = sw.Backup(src='root@example.net:/etc', dst='', rsync_options=['-h'])
    target = sw.Target(name='test', config=cfg, backups=[backup], ping=['example.net'])
    directory = 'snapshots/.rsync.XXX'
    cmdgen_test(sw.gen_backup_cmds(options, target, backup, directory, link_dest), commands)


@pytest.mark.parametrize('dry_run, commands, stdout, exception', [
    (False, [
        (CMD.mkdir('snapshots', parents=True), None),
        (CMD.subprocess(['/bin/ping', '-w1', '-c1', 'example.net'], quiet=True), 1),
    ], "Host example.net is not up, skipping sync for target test\n", None),
    (False, [
        (CMD.mkdir('snapshots', parents=True), None),
        (CMD.subprocess(['/bin/ping', '-w1', '-c1', 'example.net'], quiet=True), 0),
        (CMD.mkdtemp(prefix='rsync.', dir='snapshots'), 'snapshots/rsync.XXX'),
        (CMD.mkdir('snapshots/rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync', 'root@example.net:/etc', 'snapshots/rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), 0),
        (CMD.touch('snapshots/rsync.XXX'), None),
        (CMD.chmod('snapshots/rsync.XXX', 0o755), None),
        (CMD.rename('snapshots/rsync.XXX', 'snapshots/123456'), None),
    ], "Creating new snapshot for target test at 1970-01-02 10:17\n", None),
    (False, [
        (CMD.mkdir('snapshots', parents=True), None),
        (CMD.subprocess(['/bin/ping', '-w1', '-c1', 'example.net'], quiet=True), 0),
        (CMD.mkdtemp(prefix='rsync.', dir='snapshots'), 'snapshots/rsync.XXX'),
        (CMD.mkdir('snapshots/rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync', 'root@example.net:/etc', 'snapshots/rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), sw.CommandError("command failed")),
        (CMD.rmtree('snapshots/rsync.XXX', ignore_errors=True), None),
    ], "Creating new snapshot for target test at 1970-01-02 10:17\n", sw.CommandError),
    (True, [
        (CMD.mkdir('snapshots', parents=True), None),
        (CMD.subprocess(['/bin/ping', '-w1', '-c1', 'example.net'], quiet=True), 0),
        (CMD.mkdtemp(prefix='rsync.', dir='snapshots'), 'snapshots/rsync.XXX'),
        (CMD.mkdir('snapshots/rsync.XXX/', parents=True), None),
        (CMD.subprocess(['/usr/bin/rsync', '--dry-run', 'root@example.net:/etc', 'snapshots/rsync.XXX/'],
            rc_ok=sw.RSYNC_SUCCESS), 0),
        (CMD.rmdir('snapshots/rsync.XXX'), None),
    ], "Creating new snapshot for target test at 1970-01-02 10:17\n", None),
])
def test_gen_sync_cmds(capsys, dry_run, commands, stdout, exception):
    cfg = default_config._replace(
        rsync_bin='/usr/bin/rsync',
        rsync_defaults=[],
        rsync_exclude=[],
        ping_cmd=['/bin/ping', '-w1', '-c1'],
    )
    options = sw.DEFAULT_OPTIONS._replace(dry_run=dry_run)
    backup = sw.Backup(src='root@example.net:/etc', dst='', rsync_options=[])
    target = sw.Target(name='test', config=cfg, backups=[backup], ping=['example.net'])
    timestamp = 123456.78
    if exception is not None:
        with assert_raises(exception):
            cmdgen_test(sw.gen_sync_cmds(options, target, timestamp), commands)
    else:
        cmdgen_test(sw.gen_sync_cmds(options, target, timestamp), commands)
    captured = capsys.readouterr()
    assert captured.out == stdout


@pytest.mark.parametrize('dry_run, commands, result', [
    (False, [
        (CMD.rename('snapshots/1235', 'snapshots/1235.remove'), None),
        (CMD.rename('snapshots/1234', 'snapshots/1234.remove'), None),
    ], ['snapshots/1235.remove', 'snapshots/1234.remove']),
    (True, [], []),
])
def test_gen_rotate_cmds(dry_run, commands, result):
    cfg = default_config._replace(
        retain_hourly=1,
        retain_daily=0,
        retain_weekly=0,
        retain_monthly=0,
        retain_yearly=0,
    )
    options = sw.DEFAULT_OPTIONS._replace(dry_run=dry_run)
    target = sw.Target(name='test', config=cfg, backups=[], ping=[])
    snapshots = [
        sw.Snapshot.from_path('snapshots/1235'),
        sw.Snapshot.from_path('snapshots/1236'),
        sw.Snapshot.from_path('snapshots/1234'),
    ]
    assert cmdgen_test(sw.gen_rotate_cmds(options, target, snapshots), commands) == result


def test_gen_update_symlink_cmds():
    cfg = default_config._replace(link_fmt='%Y-%m-%dT%H:%M')
    links = ['a', 'b']
    target = sw.Target(name='test', config=cfg, backups=[], ping=[])
    snapshots = [
        sw.Snapshot.from_path('snapshots/1235'),
        sw.Snapshot.from_path('snapshots/1234'),
        sw.Snapshot.from_path('snapshots/1294'),
    ]
    commands = [
        (CMD.unlink('a'), None),
        (CMD.unlink('b'), None),
        (CMD.symlink('snapshots/1235', '1970-01-01T00:20'), None),
        (CMD.symlink('snapshots/1294', '1970-01-01T00:21'), None),
    ]
    cmdgen_test(sw.gen_update_symlink_cmds(target, links, snapshots), commands)



def _make_snapshot(dt):
    return sw.Snapshot.from_path('snapthots/%d' % dt.timestamp())


def test_rotate():
    cfg = default_config._replace(retain_hourly=2, retain_yearly=1)
    snapshots = [
        _make_snapshot(datetime(2019, 12, 1, 13, 0)),
        _make_snapshot(datetime(2019, 12, 1, 12, 0)),
        _make_snapshot(datetime(2019, 12, 1, 11, 0)),
        _make_snapshot(datetime(2019, 12, 1, 10, 0)),
    ]
    keep, remove = sw.rotate(cfg, snapshots)
    hourly, daily, weekly, monthly, yearly = keep
    assert hourly  == snapshots[0:2]
    assert daily   == [snapshots[-1]]
    assert weekly  == [snapshots[-1]]
    assert monthly == [snapshots[-1]]
    assert yearly  == [snapshots[-1]]
    assert remove  == snapshots[2:3]
