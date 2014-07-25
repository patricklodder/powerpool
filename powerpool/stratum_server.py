import json
import socket
import datetime
import argparse
import struct
import random
import time

from binascii import hexlify, unhexlify
from cryptokit import target_from_diff, uint256_from_str
from cryptokit.base58 import get_bcaddress_version
from hashlib import sha256
from gevent import sleep, with_timeout, spawn
from gevent.queue import Queue
from gevent.pool import Pool
from hashlib import sha1
from os import urandom
from pprint import pformat

from .server import GenericServer, GenericClient
from .agent_server import AgentServer
from .utils import recursive_update


from ctypes import *
class tcp_info(Structure):
         _fields_  = [
                ('tcpi_state',c_uint8),
                ('tcpi_ca_state',c_uint8),
                ('tcpi_retransmits',c_uint8),
                ('tcpi_probes',c_uint8),
                ('tcpi_backoff',c_uint8),
                ('tcpi_options',c_uint8),
                ('tcpi_snd_wscale',c_uint8,4),
                  ('tcpi_rcv_wscale',c_uint8,4),
                ('tcpi_rto',c_uint32),
                ('tcpi_ato',c_uint32),
                ('tcpi_snd_mss',c_uint32),
                ('tcpi_rcv_mss',c_uint32),
                ('tcpi_unacked',c_uint32),
                ('tcpi_sacked',c_uint32),
                ('tcpi_lost',c_uint32),
                ('tcpi_retrans',c_uint32),
                ('tcpi_fackets',c_uint32),
                ('tcpi_last_data_sent',c_uint32),
                ('tcpi_last_ack_sent',c_uint32),
                ('tcpi_last_data_recv',c_uint32),
                ('tcpi_last_ack_recv',c_uint32),
                ('tcpi_pmtu',c_uint32),
                ('tcpi_rcv_ssthresh',c_uint32),
                ('tcpi_rtt',c_uint32),
                ('tcpi_rttvar',c_uint32),
                ('tcpi_snd_ssthresh',c_uint32),
                ('tcpi_snd_cwnd',c_uint32),
                ('tcpi_advmss',c_uint32),
                ('tcpi_reordering',c_uint32),
                ('tcpi_rcv_rtt',c_uint32),
                ('tcpi_rcv_space',c_uint32),
                ('tcpi_total_retrans',c_uint32)
                  ]


class StratumManager(object):
    """ Manages the stratum servers and keeps lookup tables for addresses. """

    one_min_stats = ['reject_low', 'reject_dup', 'reject_stale',
                     'stratum_connects', 'stratum_disconnects',
                     'agent_connects', 'agent_disconnects',
                     'reject_low_shares', 'reject_dup_shares',
                     'reject_stale_shares', 'not_subbed_err', 'not_authed_err',
                     'unk_err']
    one_sec_stats = ['valid', 'valid_shares']

    def _set_config(self, **config):
        self.config = dict(aliases={},
                           vardiff=dict(spm_target=20,
                                        interval=30,
                                        tiers=[8, 16, 32, 64, 96, 128, 192, 256, 512]),
                           push_job_interval=30,
                           donate_address='',
                           idle_worker_threshold=300,
                           idle_worker_disconnect_threshold=3600,
                           agent=dict(enabled=False,
                                      port_diff=1111,
                                      timeout=120,
                                      accepted_types=['temp', 'status', 'hashrate', 'thresholds']))
        recursive_update(self.config, config)

        if not get_bcaddress_version(self.config['donate_address']):
            self.logger.error("No valid donation address configured! Exiting.")
            exit()

    def __init__(self, server, **config):
        """ Handles starting all stratum servers and agent servers, along
        with detecting algorithm module imports """
        self.server = server
        self.logger = server.register_logger('stratum_manager')
        self._set_config(**config)

        # A dictionary of all connected clients indexed by id
        self.clients = {}
        self.agent_clients = {}
        # A dictionary of lists of connected clients indexed by address
        self.address_lut = {}
        # A dictionary of lists of connected clients indexed by address and
        # worker tuple
        self.address_worker_lut = {}
        # each of our stream server objects for trickling down a shutdown
        # signal
        self.stratum_servers = []
        self.agent_servers = []
        # counters that allow quick display of these numbers. stratum only
        self.authed_clients = 0
        self.idle_clients = 0
        # Unique client ID counters for stratum and agents
        self.stratum_id_count = 0
        self.agent_id_count = 0

        self.server = server
        self.server.register_stat_counters(self.one_min_stats, self.one_sec_stats)

        # Detect and load all the hash functions we can find
        self.algos = {}
        try:
            from drk_hash import getPoWHash
        except ImportError:
            pass
        else:
            self.logger.info("Enabling x11 hashing algorithm module")
            self.algos['x11'] = getPoWHash

        try:
            from ltc_scrypt import getPoWHash
        except ImportError:
            pass
        else:
            self.logger.info("Enabling scrypt hashing algorithm module")
            self.algos['scrypt'] = getPoWHash

        try:
            from vtc_scrypt import getPoWHash
        except ImportError:
            pass
        else:
            self.logger.info("Enabling scrypt-n hashing algorithm module")
            self.algos['scryptn'] = getPoWHash

        # create a single default stratum server if none are defined
        if not self.config['interfaces']:
            self.config['interfaces'].append({})

        # Start up and bind our servers!
        for cfg in self.config['interfaces']:
            # Start a corresponding agent server
            if self.config['agent']['enabled']:
                serv = AgentServer(server, self, stratum_config=cfg, **self.config['agent'])
                self.agent_servers.append(serv)
                serv.start()

            serv = StratumServer(server, self, **cfg)
            self.stratum_servers.append(serv)
            serv.start()

    @property
    def share_percs(self):
        """ Pretty display of what percentage each reject rate is. Counts
        from beginning of server connection """
        acc_tot = self.server['valid'].total or 1
        low_tot = self.server['reject_low'].total
        dup_tot = self.server['reject_dup'].total
        stale_tot = self.server['reject_stale'].total
        return dict(
            low_perc=low_tot / float(acc_tot + low_tot) * 100.0,
            stale_perc=stale_tot / float(acc_tot + stale_tot) * 100.0,
            dup_perc=dup_tot / float(acc_tot + dup_tot) * 100.0,
        )

    @property
    def status(self):
        """ For display in the http monitor """
        dct = dict(share_percs=self.share_percs,
                   mhps=(self.server.jobmanager.config['hashes_per_share'] *
                         self.server['valid'].minute / 1000000 / 60.0),
                   agent_client_count=len(self.agent_clients),
                   client_count=len(self.clients),
                   address_count=len(self.address_lut),
                   address_worker_count=len(self.address_lut),
                   client_count_authed=self.authed_clients,
                   client_count_active=len(self.clients) - self.idle_clients,
                   client_count_idle=self.idle_clients)
        dct.update({key: self.server[key].summary()
                    for key in self.one_min_stats + self.one_sec_stats})
        return dct

    def set_conn(self, client):
        """ Called when a new connection is recieved by stratum """
        self.clients[client.id] = client

    def set_user(self, client):
        """ Add the client (or create) appropriate worker and address trackers
        """
        user_worker = (client.address, client.worker)
        self.address_worker_lut.setdefault(user_worker, [])
        self.address_worker_lut[user_worker].append(client)

        self.address_lut.setdefault(user_worker[0], [])
        self.address_lut[user_worker[0]].append(client)

    def remove_client(self, client):
        """ Manages removing the StratumClient from the luts """
        del self.clients[client.id]
        address, worker = client.address, client.worker

        # it won't appear in the luts if these values were never set
        if address is None and worker is None:
            return

        # wipe the client from the address tracker
        if address in self.address_lut:
            # remove from lut for address
            self.address_lut[address].remove(client)
            # if it's the last client in the object, delete the entry
            if not len(self.address_lut[address]):
                del self.address_lut[address]

        # wipe the client from the address/worker tracker
        key = (address, worker)
        if key in self.address_worker_lut:
            self.address_worker_lut[key].remove(client)
            # if it's the last client in the object, delete the entry
            if not len(self.address_worker_lut[key]):
                del self.address_worker_lut[key]


class ArgumentParserError(Exception):
    pass


class ThrowingArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentParserError(message)


password_arg_parser = ThrowingArgumentParser()
password_arg_parser.add_argument('-d', '--diff', type=int)


class StratumServer(GenericServer):
    """ A single port binding of our stratum server. """

    def _set_config(self, **config):
        self.config = dict(address="127.0.0.1", start_difficulty=128,
                           vardiff=True, port=3333)
        self.config.update(config)

    def __init__(self, server, stratum_manager, **config):
        self._set_config(**config)
        listener = (self.config['address'], self.config['port'])
        super(GenericServer, self).__init__(listener, spawn=Pool())
        self.server = server
        self.stratum_manager = stratum_manager
        self.logger = server.register_logger('stratum_server_{}'.
                                             format(self.config['port']))

    def start(self, *args, **kwargs):
        self.logger.info("Stratum server starting up on {address}:{port}"
                         .format(**self.config))
        GenericServer.start(self, *args, **kwargs)

    def stop(self, *args, **kwargs):
        self.logger.info("Stratum server {address}:{port} stopping"
                         .format(**self.config))
        GenericServer.stop(self, *args, **kwargs)

    def handle(self, sock, address):
        """ A new connection appears on the server, so setup a new StratumClient
        object to manage it. """
        self.server['stratum_connects'].incr()
        self.stratum_manager.stratum_id_count += 1
        StratumClient(sock, address, self.stratum_manager.stratum_id_count, self.server, self)


class StratumClient(GenericClient):
    """ Object representation of a single stratum connection to the server. """

    # Stratum error codes
    errors = {20: 'Other/Unknown',
              21: 'Job not found (=stale)',
              22: 'Duplicate share',
              23: 'Low difficulty share',
              24: 'Unauthorized worker',
              25: 'Not subscribed'}
    # enhance readability by reducing magic number use...
    STALE_SHARE_ERR = 21
    LOW_DIFF_ERR = 23
    DUP_SHARE_ERR = 22

    # constansts for share submission outcomes. returned by the share checker
    BLOCK_FOUND = 0
    VALID_SHARE = 0
    DUP_SHARE = 1
    LOW_DIFF = 2
    STALE_SHARE = 3

    def __init__(self, sock, address, id, server, stratum_server):
        self.logger = stratum_server.logger
        self.logger.info("Recieving stratum connection from addr {} on sock {}"
                         .format(address, sock))

        # Seconds before sending keepalive probes
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPIDLE, 120)
        # Interval in seconds between keepalive probes
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPINTVL, 1)
        # Failed keepalive probles before declaring other end dead
        sock.setsockopt(socket.SOL_TCP, socket.TCP_KEEPCNT, 5)

        # global items, allows access to other modules in PowerPool
        self.server = server
        self.config = stratum_server.config
        self.manager_config = stratum_server.stratum_manager.config
        self.algos = stratum_server.stratum_manager.algos
        self.jobmanager = server.jobmanager
        self.reporter = server.reporter
        self.stratum_manager = server.stratum_manager

        # register client into the client dictionary
        self.sock = sock

        # flags for current connection state
        self._disconnected = False
        self.authenticated = False
        self.subscribed = False
        self.idle = False
        self.address = None
        self.worker = None
        # the worker id. this is also extranonce 1
        self.id = hexlify(struct.pack('Q', id))
        # subscription id for difficulty on stratum
        self.subscr_difficulty = None
        # subscription id for work notif on stratum
        self.subscr_notify = None

        # running total for vardiff
        self.accepted_shares = 0
        # an index of jobs and their difficulty
        self.job_mapper = {}
        self.job_counter = 0
        # last time we sent graphing data to the server
        self.time_seed = random.uniform(0, 10)  # a random value to jitter timings by
        self.last_share_submit = time.time()
        self.last_job_push = time.time()
        self.last_job_id = None
        self.last_diff_adj = time.time() - self.time_seed
        self.difficulty = self.config['start_difficulty']
        # the next diff to be used by push job
        self.next_diff = self.config['start_difficulty']
        self.connection_time = int(time.time())
        self.msg_id = None

        # where we put all the messages that need to go out
        self.write_queue = Queue()
        write_greenlet = None
        self.fp = None

        try:
            self.stratum_manager.set_conn(self)
            self.peer_name = sock.getpeername()
            self.fp = sock.makefile()
            write_greenlet = spawn(self.write_loop)
            self.read_loop()
        except socket.error:
            self.logger.debug("Socket error closing connection", exc_info=True)
        except Exception:
            self.logger.error("Unhandled exception!", exc_info=True)
        finally:
            if self.authenticated:
                self.stratum_manager.authed_clients -= 1

            if self.idle:
                self.stratum_manager.idle_clients -= 1

            if write_greenlet:
                write_greenlet.kill()

            # handle clean disconnection from client
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except socket.error:
                pass
            try:
                if self.fp:
                    self.fp.close()
                self.sock.close()
            except (socket.error, AttributeError):
                pass

            self.server['stratum_disconnects'].incr()
            self.stratum_manager.remove_client(self)

            self.logger.info("Closing connection for client {}".format(self.id))

    @property
    def summary(self):
        """ Displayed on the all client view in the http status monitor """
        return dict(worker=self.worker,
                    idle=self.idle)

    @property
    def last_share_submit_delta(self):
        return datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(self.last_share_submit)

    @property
    def details(self):
        """ Displayed on the single client view in the http status monitor """
        fmt = "B"*7+"I"*21
        info = tcp_info(*struct.unpack(fmt, self.sock.getsockopt(socket.SOL_TCP, socket.TCP_INFO, 92)))
        return dict(alltime_accepted_shares=self.accepted_shares,
                    difficulty=self.difficulty,
                    worker=self.worker,
                    id=self.id,
                    info={k[0]: getattr(info, k[0]) for k in info._fields_},
                    last_share_submit=str(self.last_share_submit_delta),
                    idle=self.idle,
                    address=self.address,
                    ip_address=self.peer_name[0],
                    connection_time=str(self.connection_duration))

    def send_error(self, num=20, id_val=1):
        """ Utility for transmitting an error to the client """
        err = {'id': id_val,
               'result': None,
               'error': (num, self.errors[num], None)}
        self.logger.warn("Error number {} on ip {}".format(num, self.peer_name[0]))
        self.write_queue.put(json.dumps(err, separators=(',', ':')) + "\n")

    def send_success(self, id_val=1):
        """ Utility for transmitting success to the client """
        succ = {'id': id_val, 'result': True, 'error': None}
        self.logger.debug("success response: {}".format(pformat(succ)))
        self.write_queue.put(json.dumps(succ, separators=(',', ':')) + "\n")

    def push_difficulty(self):
        """ Pushes the current difficulty to the client. Currently this
        only happens uppon initial connect, but would be used for vardiff
        """
        send = {'params': [self.difficulty],
                'id': None,
                'method': 'mining.set_difficulty'}
        self.write_queue.put(json.dumps(send, separators=(',', ':')) + "\n")

    def push_job(self, flush=False, timeout=False):
        """ Pushes the latest job down to the client. Flush is whether
        or not he should dump his previous jobs or not. Dump will occur
        when a new block is found since work on the old block is
        invalid."""
        job = None
        while True:
            jobid = self.jobmanager.latest_job
            try:
                job = self.jobmanager.jobs[jobid]
                break
            except KeyError:
                self.logger.warn("No jobs available for worker!")
                sleep(0.1)

        if self.last_job_id == job.job_id and not timeout:
            self.logger.info("Ignoring non timeout resend of job id {} to worker {}.{}"
                             .format(job.job_id, self.address, self.worker))
            return

        # we push the next difficulty here instead of in the vardiff block to
        # prevent a potential mismatch between client and server
        if self.next_diff != self.difficulty:
            self.logger.info("Pushing diff updae {} -> {} before job for {}.{}"
                             .format(self.difficulty, self.next_diff, self.address, self.worker))
            self.difficulty = self.next_diff
            self.push_difficulty()

        self.logger.info("Sending job id {} to worker {}.{}{}"
                         .format(job.job_id, self.address, self.worker,
                                 " after timeout" if timeout else ''))

        self._push(job)

    def _push(self, job, flush=False):
        """ Abbreviated push update that will occur when pushing new block
        notifications. Mico-optimized to try and cut stale share rates as much
        as possible. """
        self.last_job_id = job.job_id
        self.last_job_push = time.time()
        # get client local job id to map current difficulty
        self.job_counter += 1
        job_id = str(self.job_counter)
        self.job_mapper[job_id] = (self.difficulty, job.job_id)
        self.write_queue.put(job.stratum_string() % (job_id, "true" if flush else "false"))

    def submit_job(self, data):
        """ Handles recieving work submission and checking that it is valid
        , if it meets network diff, etc. Sends reply to stratum client. """
        start = time.time()
        params = data['params']
        # [worker_name, job_id, extranonce2, ntime, nonce]
        # ["slush.miner1", "bf", "00000001", "504e86ed", "b2957c02"]
        if __debug__:
            self.logger.debug(
                "Recieved work submit:\n\tworker_name: {0}\n\t"
                "job_id: {1}\n\textranonce2: {2}\n\t"
                "ntime: {3}\n\tnonce: {4} ({int_nonce})"
                .format(
                    *params,
                    int_nonce=struct.unpack(str("<L"), unhexlify(params[4]))))

        if self.idle:
            self.idle = False
            self.stratum_manager.idle_clients -= 1

        self.last_share_submit = time.time()

        try:
            difficulty, jobid = self.job_mapper[data['params'][1]]
        except KeyError:
            # since we can't identify the diff we just have to assume it's
            # current diff
            self.send_error(self.STALE_SHARE_ERR, id_val=self.msg_id)
            self.server['reject_stale'].incr(self.difficulty)
            self.server['reject_stale_shares'].incr()
            return self.STALE_SHARE, self.difficulty

        # lookup the job in the global job dictionary. If it's gone from here
        # then a new block was announced which wiped it
        try:
            job = self.jobmanager.jobs[jobid]
        except KeyError:
            self.send_error(self.STALE_SHARE_ERR, id_val=self.msg_id)
            self.server['reject_stale'].incr(difficulty)
            self.server['reject_stale_shares'].incr()
            return self.STALE_SHARE, difficulty

        # assemble a complete block header bytestring
        header = job.block_header(
            nonce=params[4],
            extra1=self.id,
            extra2=params[2],
            ntime=params[3])
        # Grab the raw coinbase out of the job object before gevent can preempt
        # to another thread and change the value. Very important!
        coinbase_raw = job.coinbase.raw

        # Check a submitted share against previous shares to eliminate
        # duplicates
        share = (self.id, params[2], params[4], params[3])
        if share in job.acc_shares:
            self.logger.info("Duplicate share rejected from worker {}.{}!"
                             .format(self.address, self.worker))
            self.send_error(self.DUP_SHARE_ERR, id_val=self.msg_id)
            self.server['reject_dup'].incr(difficulty)
            self.server['reject_dup_shares'].incr()
            return self.DUP_SHARE, difficulty

        job_target = target_from_diff(difficulty, job.diff1)
        hash_int = uint256_from_str(self.algos[job.algo](header))
        if hash_int >= job_target:
            self.logger.info("Low diff share rejected from worker {}.{}!"
                             .format(self.address, self.worker))
            self.send_error(self.LOW_DIFF_ERR, id_val=self.msg_id)
            self.server['reject_low'].incr(difficulty)
            self.server['reject_low_shares'].incr()
            return self.LOW_DIFF, difficulty

        # we want to send an ack ASAP, so do it here
        self.send_success(id_val=self.msg_id)
        self.logger.debug("Valid share accepted from worker {}.{}!"
                          .format(self.address, self.worker))
        # Add the share to the accepted set to check for dups
        job.acc_shares.add(share)
        self.server['valid'].incr(difficulty)
        self.server['valid_shares'].incr()

        # Some coins use POW function to do blockhash, while others use SHA256.
        # Allow toggling
        if job.pow_block_hash:
            header_hash = self.algos[job.algo](header)[::-1]
        else:
            header_hash = sha256(sha256(header).digest()).digest()[::-1]
        hash_hex = hexlify(header_hash)

        # check each aux chain for validity
        for chain_id, data in job.merged_data.iteritems():
            if hash_int <= data['target']:
                self.jobmanager.found_merged_block(self.address,
                                                   self.worker,
                                                   hash_hex,
                                                   header,
                                                   job.job_id,
                                                   coinbase_raw,
                                                   data['type'])

        # valid network hash?
        if hash_int > job.bits_target:
            return self.VALID_SHARE, difficulty

        self.jobmanager.found_block(coinbase_raw,
                                    self.address,
                                    self.worker,
                                    hash_hex,
                                    header,
                                    job.job_id,
                                    start)

        return self.BLOCK_FOUND, difficulty

    def authenticate(self, data):
        try:
            password = data.get('params', [None])[1]
        except IndexError:
            password = ""

        # allow the user to use the password field as an argument field
        try:
            args = password_arg_parser.parse_args(password.split())
        except ArgumentParserError:
            pass
        else:
            if args.diff and args.diff in self.manager_config['vardiff']['tiers']:
                self.difficulty = args.diff
                self.next_diff = args.diff

        username = data.get('params', [None])[0]
        self.logger.info("Authentication request from {} for username {}"
                         .format(self.peer_name[0], username))
        user_worker = self.convert_username(username)

        # unpack into state dictionary
        self.address, self.worker = user_worker
        self.stratum_manager.set_user(self)
        self.authenticated = True
        self.stratum_manager.authed_clients += 1

        # notify of success authing and send him current diff and latest job
        self.send_success(self.msg_id)
        self.push_difficulty()
        self.push_job()

    def recalc_vardiff(self):
        # ideal difficulty is the n1 shares they solved divided by target
        # shares per minute
        spm_tar = self.manager_config['vardiff']['spm_target']
        tracker = self.reporter.addresses.get(self.address)
        if not tracker:
            self.logger.debug("VARDIFF: No address tracker, must be no valid shares for this user")
            ideal_diff = 0.0
        else:
            ideal_diff = tracker.spm / spm_tar
        self.logger.debug("VARDIFF: Calculated client {} ideal diff {}"
                          .format(self.id, ideal_diff))
        # find the closest tier for them
        new_diff = min(self.manager_config['vardiff']['tiers'], key=lambda x: abs(x - ideal_diff))

        if new_diff != self.difficulty:
            self.logger.info(
                "VARDIFF: Moving to D{} from D{} on {}.{}"
                .format(new_diff, self.difficulty, self.address, self.worker))
            self.next_diff = new_diff
        else:
            self.logger.debug("VARDIFF: Not adjusting difficulty, already "
                              "close enough")

        self.last_diff_adj = time.time()
        self.push_job(timeout=True)

    def subscribe(self, data):
        """ Performs stratum subscription logic """
        self.subscr_notify = sha1(urandom(4)).hexdigest()
        self.subscr_difficulty = sha1(urandom(4)).hexdigest()
        ret = {'result':
               ((("mining.set_difficulty",
                  self.subscr_difficulty),
                 ("mining.notify",
                  self.subscr_notify)),
                self.id,
                self.jobmanager.config['extranonce_size']),
               'error': None,
               'id': self.msg_id}
        self.subscribed = True
        self.logger.debug("Sending subscribe response: {}".format(pformat(ret)))
        self.write_queue.put(json.dumps(ret) + "\n")

    def read_loop(self):
        while True:
            if self._disconnected:
                self.logger.debug("Read loop encountered disconnect flag from "
                                  "write, exiting")
                break

            # designed to time out approximately "push_job_interval" after
            # the user last recieved a job. Some miners will consider the
            # mining server dead if they don't recieve something at least once
            # a minute, regardless of whether a new job is _needed_. This
            # aims to send a job _only_ as often as needed
            line = with_timeout(time.time() - self.last_job_push + self.manager_config['push_job_interval'] - self.time_seed,
                                self.fp.readline,
                                timeout_value='timeout')

            # push a new job if we timed out
            if line == 'timeout':
                if not self.idle and (time.time() - self.last_share_submit) > self.manager_config['idle_worker_threshold']:
                    self.idle = True
                    self.stratum_manager.idle_clients += 1

                if (time.time() - self.last_share_submit) > self.manager_config['idle_worker_disconnect_threshold']:
                    self.logger.info("Disconnecting worker {}.{} at ip {} for inactivity"
                                     .format(self.address, self.worker, self.peer_name[0]))
                    break

                if (self.authenticated is True and  # don't send to non-authed
                    # force send if we need to push a new difficulty
                    (self.next_diff != self.difficulty or
                     # send if we're past the push interval
                     time.time() > (self.last_job_push +
                                    self.manager_config['push_job_interval'] -
                                    self.time_seed))):
                    if self.config['vardiff']:
                        self.recalc_vardiff()
                    self.push_job(timeout=True)
                continue

            line = line.strip()

            # if there's data to read, parse it as json
            if len(line):
                try:
                    data = json.loads(line)
                except ValueError:
                    self.logger.warn("Data {}.. not JSON".format(line[:15]))
                    self.send_error()
                    continue
            else:
                # otherwise disconnect. Reading from a defunct connection yeilds
                # an EOF character which gets stripped off
                break

            # set the msgid
            self.msg_id = data.get('id', 1)
            if __debug__:
                self.logger.debug("Data {} recieved on client {}"
                                  .format(data, self.id))

            # run a different function depending on the action requested from
            # user
            if 'method' in data:
                meth = data['method'].lower()
                if meth == 'mining.subscribe':
                    if self.subscribed is True:
                        self.send_error()
                        continue

                    self.subscribe(data)
                elif meth == "mining.authorize":
                    if self.subscribed is False:
                        self.server['not_subbed_err'].incr()
                        self.send_error(25)
                        continue
                    if self.authenticated is True:
                        self.server['not_authed_err'].incr()
                        self.send_error(24)
                        continue

                    self.authenticate(data)
                elif meth == "mining.submit":
                    if self.authenticated is False:
                        self.server['not_authed_err'].incr()
                        self.send_error(24)
                        continue

                    outcome, diff = self.submit_job(data)
                    if self.VALID_SHARE == outcome or self.BLOCK_FOUND == outcome:
                        self.accepted_shares += diff

                    # log the share results for aggregation and transmission
                    self.reporter.log_share(self.address, self.worker, diff, outcome, job)

                    # don't recalc their diff more often than interval
                    if (self.config['vardiff'] and
                            (time.time() - self.last_diff_adj) > self.manager_config['vardiff']['interval']):
                        self.recalc_vardiff()
                else:
                    self.logger.warn("Unkown action {} for command {}"
                                     .format(data['method'][:20], self.peer_name[0]))
                    self.server['unk_err'].incr()
                    self.send_error(id_val=self.msg_id)
            else:
                self.logger.warn("Empty action in JSON {}"
                                 .format(self.peer_name[0]))
                self.server['unk_err'].incr()
                self.send_error(id_val=self.msg_id)
