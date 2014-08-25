#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# bitshares_delegate_tools - Tools to easily manage the bitshares client
# Copyright (c) 2014 Nicolas Wack <wackou@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from collections import deque
from datetime import datetime
from .core import config, StatsFrame
from .notification import send_notification
from .rpcutils import nodes
from .process import bts_process
import time
import logging

log = logging.getLogger(__name__)

cfg = config['monitoring']

# make sure we don't have huge plots that take forever to render
maxlen = 2000

time_span = cfg['time_span']
growing_time_interval = cfg['time_interval']
desired_maxlen = int(time_span / growing_time_interval)
stable_time_interval = growing_time_interval

if desired_maxlen > maxlen:
    stable_time_interval *= (desired_maxlen / maxlen)

time_interval = growing_time_interval

stats = deque(maxlen=min(desired_maxlen, maxlen))


def monitoring_thread():
    global time_interval

    # find node object to be monitored
    for n in nodes:
        if n.host == config['monitoring']['host']:
            node = n
            break
    else:
        raise ValueError('"%s" is not a valid host name. Available: %s' % (config['monitor_host'], ', '.join(n.host for n in nodes)))

    last_state = None
    last_state_consecutive = 0
    last_stable_state = None
    connection_status = None
    last_producing = True
    missed_count = 0

    log.info('Starting monitoring thread')

    while True:
        if time_interval != stable_time_interval and len(stats) == stats.maxlen:
            log.debug('Switching stats plot to new time interval: %s instead of %s during initial fill up' %
                      (stable_time_interval, growing_time_interval))
            time_interval = stable_time_interval
        time.sleep(time_interval)
        log.debug('-------- Monitoring status of the BitShares client --------')
        node.clear_rpc_cache()

        try:
            if not node.is_online():
                log.debug('Offline')
                if last_state == 'online':
                    last_state_consecutive = 0
                last_state = 'offline'
                last_state_consecutive += 1

                # wait for 3 "confirmations" that we are offline, to avoid
                # reacting too much on temporary connection errors
                if last_state_consecutive == 3:
                    if last_stable_state and last_stable_state != last_state:
                        log.warning('Delegate just went offline...')
                        send_notification('Delegate just went offline...', alert=True)
                    last_stable_state = last_state

                stats.append(StatsFrame(cpu=0, mem=0, connections=0, timestamp=datetime.utcnow()))
                continue

            log.debug('Online')
            if last_state == 'offline':
                last_state_consecutive = 0
            last_state = 'online'
            last_state_consecutive += 1

            # wait for 3 "confirmations" that we are online, to avoid
            # reacting too much on temporary connection errors
            if last_state_consecutive == 3:
                if last_stable_state and last_stable_state != last_state:
                    log.info('Delegate just came online!')
                    send_notification('Delegate just came online!')
                last_stable_state = last_state


            # check for minimum number of connections for delegate to produce
            info = node.get_info()
            if info['network_num_connections'] <= 5:
                if connection_status == 'connected':
                    log.warning('Fewer than 5 network connections...')
                    send_notification('Fewer than 5 network connections...', alert=True)
                    connection_status = 'starved'
            else:
                if connection_status == 'starved':
                    log.info('Got more than 5 connections now')
                    send_notification('Got more than 5 connections now')
                    connection_status = 'connected'

            # monitor for missed blocks
            if info['blockchain_head_block_age'] < 60:  # only monitor if synced
                producing, n = node.get_streak()
                if last_producing:
                    if not producing:
                        missed_count += 1
                        if missed_count == 3:
                            # wait for 3 confirmations before finding a miss, to
                            # avoid reacting too quick on glitches
                            log.warning('Missed a block!')
                            send_notification('Missed a block!', alert=True)
                        else:
                            # we still consider we're producing
                            producing = True
                    else:
                        missed_count = 0

                last_producing = producing


            # only monitor cpu and network if we are monitoring localhost
            if node.host != 'localhost':
                continue

            p = bts_process() # p should not be None otherwise we would be offline
            s = StatsFrame(cpu=p.cpu_percent(),
                           mem=p.memory_info().rss,
                           connections=info['network_num_connections'],
                           timestamp=datetime.utcnow())

            log.debug('appending to stats: %s' % hex(id(stats)))
            stats.append(s)
            log.debug('stats len: %d' % len(stats))

        except Exception as e:
            log.error('An exception occurred in the monitoring thread:')
            log.exception(e)
