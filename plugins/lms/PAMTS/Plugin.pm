package Plugins::PAMTS::Plugin;

# PAMTS play-history exporter for Lyrion Music Server.
#
# Why this plugin exists
# ----------------------
# LMS records play counts and last-played times in its persistent database, and that
# data deliberately survives library rescans -- which makes it exactly the right signal
# for deciding what belongs on fast storage. But NONE of it is reachable over the
# server's API: neither the `titles` nor the `songinfo` query returns playcount or
# lastplayed, with any documented tag.
#
# The alternative is for an external tool to open persist.db itself, which means giving
# it filesystem access to this host and coupling it to a schema it does not own. This
# plugin removes that need. It runs in-process, where the data is legitimately available,
# and publishes it as an ordinary CLI query -- so it is automatically available over the
# same JSON-RPC endpoint every other query uses, with no new port, no new credentials
# and no new transport.
#
# Queries added
# -------------
#   pamts info ?
#       Counts, so a client can sanity-check before sweeping.
#
#   pamts history <index> <quantity> [since:<epoch>]
#       One page of played tracks, oldest play first. `since` returns only tracks played
#       after that unix timestamp, which is what makes incremental polling cheap.
#
# Example (as any other CLI query):
#   curl -X POST -H 'Content-Type: application/json' \
#     -d '{"id":1,"method":"slim.request","params":["",["pamts","history","0","500"]]}' \
#     http://localhost:9000/jsonrpc.js
#
# Read-only: this plugin never writes to the database.

use strict;
use warnings;

use base qw(Slim::Plugin::Base);

use Slim::Control::Request;
use Slim::Utils::Log;

my $log = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.pamts',
    defaultLevel => 'WARN',
    description  => 'PLUGIN_PAMTS',
});

# A page bigger than this is refused: the point of paging is to avoid building a huge
# response in memory, and a client asking for everything at once defeats it.
use constant MAX_PAGE => 5000;

sub initPlugin {
    my $class = shift;

    $class->SUPER::initPlugin(@_);

    # [needClient, isQuery, hasTags, callback]
    Slim::Control::Request::addDispatch(
        ['pamts', 'history', '_index', '_quantity'],
        [0, 1, 1, \&historyQuery]);

    Slim::Control::Request::addDispatch(
        ['pamts', 'info', '?'],
        [0, 1, 0, \&infoQuery]);

    $log->info('PAMTS: registered "pamts history" and "pamts info" CLI queries');
}

sub getDisplayName { 'PLUGIN_PAMTS' }

# tracks_persistent and tracks live on the same database handle (persist.db is attached),
# so they can be joined directly. Raw SQL rather than the ORM because this runs over tens
# of thousands of rows and a per-row lookup for the file size would be far slower.
sub _dbh { return Slim::Schema->dbh }

sub infoQuery {
    my $request = shift;

    if (!$request->isQuery([['pamts'], ['info']])) {
        $request->setStatusBadDispatch();
        return;
    }

    my $ok = eval {
        my $dbh = _dbh();
        my ($total)  = $dbh->selectrow_array('SELECT COUNT(*) FROM tracks_persistent');
        my ($played) = $dbh->selectrow_array(
            'SELECT COUNT(*) FROM tracks_persistent WHERE lastplayed IS NOT NULL AND lastplayed > 0');
        my ($newest) = $dbh->selectrow_array('SELECT MAX(lastplayed) FROM tracks_persistent');

        $request->addResult('tracks', $total  || 0);
        $request->addResult('played', $played || 0);
        $request->addResult('newest_lastplayed', $newest || 0);
        $request->addResult('max_page', MAX_PAGE);
        $request->addResult('version', '0.1.0');
        1;
    };
    if (!$ok) {
        $log->error("PAMTS: info query failed: $@");
        $request->addResult('error', 'query failed');
    }

    $request->setStatusDone();
}

sub historyQuery {
    my $request = shift;

    if (!$request->isQuery([['pamts'], ['history']])) {
        $request->setStatusBadDispatch();
        return;
    }

    my $index    = $request->getParam('_index')    || 0;
    my $quantity = $request->getParam('_quantity') || 0;
    my $since    = $request->getParam('since')     || 0;

    # Guard the inputs: these arrive from the network.
    $index    = 0 if $index    !~ /^\d+$/;
    $quantity = 0 if $quantity !~ /^\d+$/;
    $since    = 0 if $since    !~ /^\d+$/;
    $quantity = MAX_PAGE if !$quantity || $quantity > MAX_PAGE;

    my $ok = eval {
        my $dbh = _dbh();

        my ($count) = $dbh->selectrow_array(
            'SELECT COUNT(*) FROM tracks_persistent WHERE lastplayed IS NOT NULL AND lastplayed > ?',
            undef, $since);
        $count ||= 0;
        $request->addResult('count', $count);

        if ($count) {
            # Oldest play first, with id as a tie-breaker so paging is stable when many
            # tracks share a timestamp -- without that, rows can be skipped or repeated
            # between pages.
            my $sth = $dbh->prepare(q{
                SELECT tp.url, tp.lastplayed, tp.playcount, t.filesize
                FROM tracks_persistent tp
                LEFT JOIN tracks t ON t.urlmd5 = tp.urlmd5
                WHERE tp.lastplayed IS NOT NULL AND tp.lastplayed > ?
                ORDER BY tp.lastplayed ASC, tp.id ASC
                LIMIT ? OFFSET ?
            });
            $sth->execute($since, $quantity, $index);

            my $n = 0;
            while (my $row = $sth->fetchrow_arrayref) {
                my ($url, $lastplayed, $playcount, $filesize) = @$row;
                next unless defined $url;
                $request->addResultLoop('history_loop', $n, 'url', $url);
                $request->addResultLoop('history_loop', $n, 'lastplayed', $lastplayed + 0);
                $request->addResultLoop('history_loop', $n, 'playcount', ($playcount || 0) + 0);
                # filesize is absent for tracks that have since left the library; the
                # play record outlives the file, which is intentional in LMS.
                $request->addResultLoop('history_loop', $n, 'filesize', ($filesize || 0) + 0);
                $n++;
            }
            $sth->finish;
        }
        1;
    };
    if (!$ok) {
        $log->error("PAMTS: history query failed: $@");
        $request->addResult('error', 'query failed');
    }

    $request->setStatusDone();
}

1;
