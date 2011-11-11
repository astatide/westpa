from __future__ import division, print_function; __metaclass__ = type

import sys, os, logging, socket, multiprocessing, threading, time, numpy
import argparse
from collections import deque
import zmq
import wemd
from wemd.work_managers import WEMDWorkManager

log = logging.getLogger(__name__)

DEFAULT_ANN_PORT     = 23811
DEFAULT_TASK_PORT    = 23812

class ZMQBase:
    def __init__(self, context=None):
        self.context = context or zmq.Context(multiprocessing.cpu_count())
        # Time (in s) for sockets to settle
        self.socket_delay = 0.1
        log.debug('ZMQ context {!r}'.format(self.context))

class ZMaster(ZMQBase):
    def __init__(self, task_endpoint, ann_endpoint):
        log.debug('initializing ZMaster with task_endpoint={!r}, ann_endpoint={!r}'.format(task_endpoint, ann_endpoint))
        ZMQBase.__init__(self)
                
        self.task_endpoint = task_endpoint
        self.ann_endpoint = ann_endpoint
        
        self.ann_socket = None
        self.task_socket = None
        
        # When to give up under the assumption that no clients are available
        self.abort_interval = 600
        self.last_contact = 0
        
        # How often (in seconds) to wake up and check for new work or the shutdown signal 
        self.check_interval = 0.1
        
        # How often (in seconds) to announce that work is available (for slow joiners)
        self.announce_interval = 10
        self.last_announcement = 0
        
        # Main loop thread
        self.thread = None
        
        # Shutdown at next opportunity?
        self.shutdown_flag = False
        self.shutdown_exit_code = 0
        
        # incoming tasks, undispatched
        self.task_queue = deque()
                        
        # Mapping of task_id to results (i.e. returned results)
        # the main loop inserts items, and wait() removes them
        self.results_queue = deque()
        self.results_avail = threading.Condition()
        
    def handle_request(self, n_tasks):
        log.debug('receiving task request')
        self.last_contact = time.time()
        to_send = []
        while len(to_send) < n_tasks:
            try:
                # Atomic pops from task_queue
                to_send.append(self.task_queue.popleft())
            except IndexError:
                break
        log.debug('sending {} task(s)'.format(len(to_send)))
        self.task_socket.send_pyobj(to_send)

    def handle_results(self, payload):
        log.debug('receiving results')
        self.last_contact = time.time()
        results = list(payload)
        self.task_socket.send('')
        for result in results:
            log.debug('received results for task {!r}'.format(result.task_id))
            self.results_queue.append(result)
        with self.results_avail:
            self.results_avail.notify_all()
                        
    def announce_shutdown(self, exit_code=0):
        '''Announce shutdown and close sockets'''
        log.debug('announcing shutdown')
        self.ann_socket.send_pyobj(('shutdown', exit_code))
        log.debug('shutting down')
        self.ann_socket.close()
        self.task_socket.close()
        self.ann_socket = None
        self.task_socket = None
        
    def main_loop(self):
        log.debug('starting main loop')
        self.ann_socket = self.context.socket(zmq.PUB)
        self.task_socket = self.context.socket(zmq.REP)
        
        self.ann_socket.bind(self.ann_endpoint)
        self.task_socket.bind(self.task_endpoint)
                
        poller = zmq.Poller()
        poller.register(self.task_socket, zmq.POLLIN)
                
        log.debug('waiting for sockets to settle')
        time.sleep(self.socket_delay)
        
        self.last_contact = time.time()
        try:
            while not self.shutdown_flag:            
                # Wait on messages from clients (or the shutdown signal)
                poll_results = set(fd for (fd,_flag) in poller.poll(self.check_interval*1000))
                
                if self.task_socket in poll_results:
                    reqtype, payload = self.task_socket.recv_pyobj()
                    
                    if reqtype == 'request':
                        self.handle_request(payload)
                    elif reqtype == 'results':
                        self.handle_results(payload)
                    elif reqtype == 'shutdown':
                        self.announce_shutdown()
                        return
                    else:
                        log.error('invalid request received')
                else:
                    # Timeout
                    if time.time() - self.last_contact >= self.abort_interval:
                        raise RuntimeError('no clients contacted server in {:d} seconds'.format(self.abort_interval))
                    
                    if self.task_queue:
                        self.announce_tasks()
            else:
                self.announce_shutdown(self.shutdown_exit_code)
                    
        except KeyboardInterrupt:
            self.announce_shutdown(2) 
        except Exception as e:
            log.error('Unhandled exception: {!s}'.format(e))
            self.announce_shutdown(4)
            raise
        
    def start_threads(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.main_loop)
            self.thread.daemon = False
            self.thread.start()

    def announce_tasks(self):
        current_time = time.time()
        qlen = len(self.task_queue)
        if qlen and (current_time - self.last_announcement >= self.announce_interval):
            log.debug('announcing tasks available')
            self.ann_socket.send_pyobj(('taskavail', self.task_endpoint))
            self.last_announcement = time.time()
        else:
            self.last_announcement = 0
        
    def reset_announce_timer(self):
        if not len(self.task_queue):
            self.last_announcement = 0
                
    def dispatch(self, task=None):        
        if task is not None:
            self.task_queue.append(task)
            self.announce_tasks()
            
    def dispatch_all(self, tasks):
        if tasks:
            for task in tasks:
                # Atomic append; extend() is probably not a good idea here
                self.task_queue.append(task)
            self.last_task_added = time.time()
            
    def shutdown(self, exit_code=0):
        self.shutdown_flag = True
        self.shutdown_exit_code = exit_code

class Task:
    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.completed = False
        self.result = None
        self.exception = None
        
    def run(self):
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.exception = e
        self.completed = True
        
ShutdownSentinel = object()
                    
class ZWorker(ZMQBase):
    def __init__(self, ann_endpoint, propagator, n_procs=1):
        log.debug('initializing ZWorker with ann_endpoint={!r}, propagator={!r}, n_procs={!r}'
                  .format(ann_endpoint, propagator, n_procs))
        # Initialize our base class to initialize the ZeroMQ context
        ZMQBase.__init__(self)
    
        self.propagator = propagator
        self.n_procs = n_procs
                
        self.ann_endpoint = ann_endpoint
        self.listen_thread = None
        self.work_threads = None        
        self.shutdown_flag = False
        self.task_queue = deque()
                                
    def shutdown(self):
        log.debug('worker shutting down')
        self.shutdown_flag = False
        
    def start_threads(self):
        self.listen_thread = threading.Thread(target=self.listen_loop)
        self.listen_thread.daemon = False
        self.listen_thread.start()
                                
    def listen_loop(self):
        '''Receive announcements and commands from the master'''
        
        log.debug('worker listening for announcements')
        
        self.ann_socket = self.context.socket(zmq.SUB)
        self.ann_socket.setsockopt(zmq.SUBSCRIBE,'')
        self.ann_socket.connect(self.ann_endpoint)
        
        poller = zmq.Poller()
        poller.register(self.ann_socket, zmq.POLLIN)
        
        while not self.shutdown_flag:
            poll_results = set(fd for (fd,_flag) in poller.poll())
                                    
            if self.ann_socket in poll_results:
                msg, payload = self.ann_socket.recv_pyobj()
                if msg == 'shutdown':
                    log.debug('shutdown received')
                    return
                elif msg == 'taskavail':
                    # For as long as we have tasks to do, do them, then await the next "work available" message
                    task_endpoint = payload
                    task_socket = self.context.socket(zmq.REQ)
                    task_socket.connect(task_endpoint)
                    tasks = True
                    try:
                        while tasks:
                            task_socket.send_pyobj(('request', self.n_procs))
                            tasks = task_socket.recv_pyobj()
                            if tasks:
                                results = self.do_tasks(tasks)
                                task_socket.send_pyobj(('results', results))
                                task_socket.recv()
                    finally:
                        task_socket.close()
                        del task_socket
                else:
                    log.error('unknown message received')
        else:
            log.debug('shutting down')
                    
        self.ann_socket.close()
                            
    def do_tasks(self, tasks):
        if len(tasks) % self.n_procs == 0:
            n_blocks = len(tasks) // self.n_procs
        else:
            n_blocks = len(tasks) // self.n_procs + 1
        log.debug('performing {:d} task(s) in {:d} block(s) of size {:d}'.format(len(tasks), n_blocks, self.n_procs))
        taskarray = numpy.empty((self.n_procs, n_blocks), numpy.object_)
        taskarray.flat[0:len(tasks)] = tasks
        threads = [TaskThread(self.propagator, taskarray[ithread,:]) for ithread in xrange(self.n_procs)]
        for thread in threads:
            thread.start()
        
        results = []
        for thread in threads:
            thread.join()
            results.extend(thread.results)

        return results
    
class ZMQWorkManager(WEMDWorkManager):
    def __init__(self):
        WEMDWorkManager.__init__()
        
        runtime_config = wemd.rc.config
        # Where we will run the master ZMQ device 
        self.master_hostname  = runtime_config.get('zmq_work_manager.master', socket.gethostname())
        # Port to which the master will make announcements (like "work available" or "exit now")
        self.ann_port  = runtime_config.get_int('zmq_work_manager.ann_port', DEFAULT_ANN_PORT)
        # Port to which clients will connect on the master to receive tasks or return results
        self.task_port    = runtime_config.get_int('zmq_work_manager.task_port', DEFAULT_TASK_PORT)
        
        self.mode = None
        
        # ZMQ communications device (Master or Worker, as appropriate for this process)
        self.zdev = None
        
        self.blocksize = 1
        
    def parse_aux_args(self, aux_args, do_help = False):
        parser = argparse.ArgumentParser(usage='%(prog)s [NON_WORK_MANAGER_OPTIONS] [OPTIONS]',
                                         add_help=False)
        
        runtime_config = wemd.rc.config
        group = parser.add_argument_group('work manager options')
        
        mode_group = group.add_mutually_exclusive_group()
        mode_group.add_argument('--master', dest='mode', action='store_const', const='master',
                                help='Run as a WEMD master (responsible for coordinating WE and parallel propagation')
        mode_group.add_argument('--worker', dest='mode', action='store_const', const='worker',
                                help='Run as a WEMD worker (listening for work from a master)')
        mode_group.set_defaults(mode='master') 
        
        group.add_argument('-n', '-np', '-nw', type=int, dest='n_workers', default=multiprocessing.cpu_count(),
                            help='Number of worker processes to run on this host. Use 0 for a dedicated server '
                                +' or forwarder process. (Default: %(default)s)')
        group.add_argument('-H', '--host', default=socket.gethostname(),
                            help='Upstream (master) host (default: %(default)s)')
        group.add_argument('--aport', type=int, default=DEFAULT_ANN_PORT,
                            help='Port on which remote workers receive announcements (default: %(default)s)')
        group.add_argument('--tport', type=int, default=DEFAULT_TASK_PORT,
                            help='Port on which remote workers contact the master to receive work (default: %(default)s)')

        if do_help:
            parser.print_help()
            sys.exit(0)
        args, extra_args = parser.parse_known_args(aux_args)
        
        # zmq expects an IP address, not a hostname, for TCP transports
        # otherwise, a cryptic "no such device" error happens
        self.upstream_host =  runtime_config['zmq_work_manager.upstream_host'] = socket.gethostbyname(args.host)
        self.ann_port =       runtime_config['zmq_work_manager.ann_port'] = args.aport
        self.task_port =      runtime_config['zmq_work_manager.task_port'] = args.tport
        self.n_workers =      runtime_config['zmq_work_manager.n_workers'] = args.n_workers
        self.mode = args.mode
        
        return extra_args
                        
    def run_worker(self):
        assert self.mode == 'worker'
        assert self.zdev is not None
        return self.zdev.listen_thread.join()
        
    def shutdown(self, exit_code = 0):
        self.shutdown_called = True
        if self.mode == 'master':
            try:
                self.zdev.shutdown(exit_code)
            except Exception as e:
                log.error('ignorning exception {!r} during shutdown()'.format(e))
                
    def start_master(self):
        assert self.mode == 'master'
        
        if self.n_workers:
            # Need to fork and set up the child as a worker
            pid = os.fork()
            if pid == 0:
                # in child
                self.mode = 'worker'
                self.start_worker()
                return
        
        # We should never reach this point in a child process
        assert self.mode == 'master'
        assert self.zdev is None
        self.zdev = ZMaster(self.task_endpoint, self.ann_endpoint)
        self.zdev.start_threads()

    def start_worker(self):
        assert self.mode == 'worker'
        if not self.n_workers:
            raise ValueError('attempting to start a dedicated worker with no subprocesses')            
        self.zdev = ZWorker(self.ann_endpoint, self.propagator, self.n_workers)
        self.zdev.start_threads()
            
    def startup(self):        
        # Connect to a specific remote host for now; maybe add wildcards for the master later
        self.ann_endpoint = 'tcp://{}:{}'.format(self.upstream_host, self.ann_port)
        self.task_endpoint = 'tcp://{}:{}'.format(self.upstream_host, self.task_port)
        
        if self.mode == 'master':
            self.start_master()
        else:
            self.start_worker()
        
        log.debug('process {:d} zdev is {!r}'.format(os.getpid(), self.zdev))
        assert self.zdev is not None
    
    def propagate(self, segments):
        log.debug('zeromq propagate() called; mode={!r}, zdev={!r}'.format(self.mode, self.zdev))
        assert self.mode == 'master'
        assert self.zdev is not None
        
        blocks = [segments[i:i+self.blocksize] for i in xrange(0,len(segments),self.blocksize)]
        log.debug('dispatching {} segment(s) in {} block(s)'.format(len(segments), len(blocks)))
        
        outgoing_by_id = {segment.seg_id: segment for segment in segments}
        outgoing_ids = set(outgoing_by_id)
        completed_ids = set()
        
        
        # label deterministically to save a little system entropy
        tasks = [Task('propagate', block, task_id=('propagate', block[0].n_iter, block[0].seg_id)) for block in blocks]
        self.zdev.dispatch_all(tasks)
        while len(completed_ids) < len(segments):
            self.zdev.results_avail.acquire()
            log.debug('waiting on results')
            self.zdev.results_avail.wait()
            while True:
                try:
                    incoming_segments = list(self.zdev.results_queue.popleft().result)                    
                except IndexError:
                    break
                else:
                    incoming_by_id = {segment.seg_id: segment for segment in incoming_segments}
                    incoming_ids = set(incoming_by_id)
                    
                    # Make sure we didn't get back something we didn't send out
                    assert (outgoing_ids & incoming_ids) == incoming_ids
                    
                    # Make sure we didn't get back a second copy of something
                    assert len(incoming_ids & completed_ids) == 0
                    
                    # Record incoming segment IDs for future checks
                    completed_ids |= incoming_ids
                    
                    for incoming_segment in incoming_segments:
                        orig_segment = outgoing_by_id[incoming_segment.seg_id]
                        orig_segment.status = incoming_segment.status
                        orig_segment.walltime = incoming_segment.walltime
                        orig_segment.cputime  = incoming_segment.cputime
                        orig_segment.pcoord[...] = incoming_segment.pcoord[...]
                    
                    if incoming_segments:
                        self.data_manager.open_backing()
                        self.data_manager.update_segments(incoming_segments[0].n_iter, incoming_segments)
                        self.data_manager.close_backing()                    

        self.zdev.reset_announce_timer()
                        
        
