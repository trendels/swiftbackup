# About

`swiftbackup` is a wrapper around `rsync` to simplify creating rotating
snapshot backups using the `rsync --link-dest` technique. With this technique,
each time a new snapshot is created, files that have not changed are
hard-linked to the same file from the previous snapshot, saving space. Old
snapshots are are deleted after a configurable amount of time.

# Installation

Swiftbackup consists of a single file. It requires only Python ≥ 3.5
and rsync.

    $ git clone https://github.com/trendels/swiftbackup
    $ sudo cp swiftbackup/swiftbackup.py /usr/local/bin/swiftbackup

# Configuration

Swiftbackup needs a configuration file. [The example configuration
file](swiftbackup.conf) has comments explaining all available options. To
get started, have swiftbackup write the `[defaults]` section and continue
from there:

    $ swiftbackup --write-config > swiftbackup.conf

The generated file looks something like this (the paths to rsync/ping might be
different on your system):

    [defaults]
    retain_hourly = 6
    retain_daily = 7
    retain_weekly = 4
    retain_monthly = 6
    retain_yearly = 0
    rsync_bin = /usr/bin/rsync
    rsync_defaults = -ax --delete --delete-excluded --numeric-ids --relative
    rsync_options = 
    rsync_exclude = 
    ping_cmd = /bin/ping -w1 -c1
    link_fmt = %Y-%m-%d.%H%M
    backup_directory = /srv/swiftbackup

The `[defaults]` section contains the built-in defaults. If you don't change
them you can also remove or comment out these settings.

For each backup target, add a new section with a `backup` setting at the end.
You can also override any of the defaults for this backup.

For example:

    [example.net]
    retain_hourly = 2
    rsync_options = --verbose --human-readable
    backup = user@example.net:/home/user

Snapshots for this target will be kept under `/srv/swiftbackup/example.net`.

You can pass the location of the configuration file using the `-c/--config`
option, or by setting the `SWIFTBACKUP_CONFIG` environment variable.
By default, `swiftbackup` will look under `/etc/swiftbackup.conf`.

Before you get started, make sure the `backup_directory` exists:

    $ sudo mkdir /srv/swiftbackup

# Backing up

You'll probably want to make the backups as `root` in order to be able to
correctly store the file permissions of the files being backed up.

New snapshots are created using `swiftbackup sync`:

    $ sudo swiftbackup -c swiftbackup.conf sync example.net
    Creating new snapshot for target example.net at 2019-12-13 10:30

List the available snapshots with `swiftbackup status`:

    $ swiftbackup -c swiftbackup.conf status
    Target       Date       Time  Week  Snapshot 
    =============================================
    example.net  2019-12-13 10:30   49  h d w m   

This tells us that we have one snapshot for the example.net target, and that
it is kept because it is an **h**ourly, **d**aily, **w**eekly and
**m**onthly snapshot.

The files are stored under `/srv/swiftbackup/example.net`:

    $ sudo ls -l /srv/swiftbackup/example.net
    total 0
    lrwxrwxrwx 1 st st 20 Dez 13 10:30 2019-12-13.1030 -> snapshots/1576229448
    drwxrwxr-x 3 st st 60 Dez 13 10:30 snapshots

    $ sudo ls /srv/swiftbackup/example.net/2019-12-13.1030/
    home

If we try to create another snapshot, `swiftbackup` will do nothing, since
we already have a snapshot for the current hour available.

    $ sudo swiftbackup -c swiftbackup.conf sync example.net
    $

You can use `-f/--force` to force creating a new snapshot:

    $ sudo swiftbackup -c swiftbackup.conf sync -f example.net
    Creating new snapshot for target example.net at 2019-12-13 10:32

    $ swiftbackup -c swiftbackup.conf status
    Target       Date       Time  Week  Snapshot 
    =============================================
    example.net  2019-12-13 10:32   49  h         
    example.net  2019-12-13 10:30   49    d w m   

The new snapshot is listed as an "hourly" snapshot in the first line. Note
that for "hourly" snapshots, swiftbackup tries to keep the latest snapshot
available for any given hour, whereas for the other snapshot types the oldest
available snapshot is kept.

After creating another snapshot with `sync -f`, the result might look
like this:

    $ swiftbackup -c swiftbackup.conf status
    Target       Date       Time  Week  Snapshot 
    =============================================
    example.net  2019-12-13 10:35   49  h         
    example.net  2019-12-13 10:32   49            
    example.net  2019-12-13 10:30   49    d w m   

The snapshot taken at 10:32 is no longer needed, since we have another
snapshot for the same hour (taken at 10:35). Snapshots are removed by calling
`swiftbackup rotate`. You can also rotate automatically after making a new
snapshot by passing the `--rotate` option to `swiftbackup sync`.

    $ swiftbackup -c swiftbackup.conf rotate example.net
    Removing snapshot for example.net from 2019-12-01 10:32

# Automating backups with cron

To back up all targets defined in the configuration file, you can use the
special name `all`:

    5 * * * *  root  /usr/local/bin/swiftbackup sync --rotate all

This creates new snapshots for all targets defined in `/etc/swiftbackup.conf`
at 5 minutes past the hour, and rotates out snapshots that are no longer
needed. It is safe to run `swiftbackup` at shorter intervals than required,
because by default no data will be copied if a target already has a suitable
snapshot for the smallest configured retention interval.

For large directory trees, removing old snapshots can easily take longer than
taking the actual snapshot itself. Therefore, you might want to run
`swiftbackup rotate` as a separate step, at a time when no backups are running:

    0  2  * * *  root  /usr/local/bin/swiftbackup sync target1
    0  3  * * *  root  /usr/local/bin/swiftbackup sync target2
    0 10  * * *  root  /usr/local/bin/swiftbackup rotate all

# More information

See `swiftbackup --help` and the example configuration file for more detailed
information on the available options on the command line and the
configuration file.

## License

swiftbackup is licensed under the MIT license. See the included file `LICENSE`
for details.
