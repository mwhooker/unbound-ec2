from collections import namedtuple
from dns import resolver, query
from unittest import TestCase
from vaurien.util import start_proxy, stop_proxy
from vaurienclient import Client as VClient

import atexit
import boto.ec2
import dns.message
import dns.name
import os.path
import shlex
import subprocess
import tempfile
import time
import vaurien.behaviors.error


UnboundConf = namedtuple('UnboundConf', ('port', 'module'))

# boto doesn't retry 501s
del vaurien.behaviors.error._ERRORS[501]
vaurien.behaviors.error._ERROR_CODES = vaurien.behaviors.error._ERRORS.keys()


def make_config(conf):
    tpl = """
server:
        interface: 127.0.0.1
        port: {conf.port}
        username: ""
        do-daemonize: no
        verbosity: 2
        directory: ""
        logfile: ""
        chroot: ""
        pidfile: ""
        module-config: "python validator iterator"


remote-control:
        control-enable: no

python:
        python-script: "{conf.module}"
"""
    return tpl.format(conf=conf)

class TestBadNetwork(TestCase):

    @staticmethod
    def _start_unbound(conf):
        nt = tempfile.NamedTemporaryFile(suffix='.conf')
        nt.write(make_config(conf))
        nt.flush()

        args = shlex.split("/usr/local/sbin/unbound -dv -c %s" % nt.name)
        time.sleep(1)
        testenv = os.environ.copy()
        testenv.update({
            'AWS_REGION': 'proxy',
            'http_proxy': 'localhost:8000',
            'UNBOUND_DEBUG': "true"
        })
        proc = subprocess.Popen(args, env=testenv)
        time.sleep(1)

        @atexit.register
        def last():
            try:
                proc.kill()
            except OSError as e:
                if e.errno != 3:
                    raise

        def finish():
            proc.terminate()
            proc.wait()
            nt.close()

        return finish

    def setUp(self):

        self.domain = "mwhooker.dev.banksimple.com."
        module = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'unbound_ec2.py')
        self.conf = UnboundConf(5003, module)

        self.unbound_stop = self._start_unbound(self.conf)

    def tearDown(self):
        self.unbound_stop()
        stop_proxy(self.proxy_pid)

    def _setup_proxy(self, protocol='http', options=None):
        # vaurien --protocol tcp --proxy localhost:8888 --backend
        # ec2.us-west-2.amazonaws.com:80 --log-level debug --protocol-tcp-reuse-socket
        # --protocol-tcp-keep-alive

        # vaurien.run --backend ec2.us-west-1.amazonaws.com:80 --proxy
        # localhost:8000 --log-level info --log-output - --protocol tcp --http
        # --http-host localhost --http-port 8080 --protocol-tcp-reuse-socket
        # --protocol-tcp-keep-alive
        if not options:
            options = []
        self.proxy_pid = start_proxy(
            protocol=protocol,
            proxy_port=8000,
            backend_host=boto.ec2.RegionData['us-west-1'],
            backend_port=80,
            options=[
                '--protocol-tcp-reuse-socket',
                '--protocol-tcp-keep-alive'
            ] + options
        )
        assert self.proxy_pid is not None

    def _query_ns(self):
        domain = dns.name.from_text(self.domain)
        if not domain.is_absolute():
            domain = domain.concatenate(dns.name.root)
        request = dns.message.make_query(domain, dns.rdatatype.ANY)

        res = query.tcp(
            request, where='127.0.0.1',
            port=self.conf.port)
        print [str(a) for a in res.answer]
        return res

    def _test_result(self, result):
        self.assertTrue(len(result.answer) == 1)
        self.assertRegexpMatches(
            result.answer[0].to_text(), 
            "%s \d+ IN A \d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}" % self.domain
        )

    def test_normal(self):
        # dig A @127.0.0.1 -p 5003 mwhooker.dev.banksimple.com.
        self._setup_proxy()
        client = VClient()

        with client.with_behavior('dummy'):
            result = self._query_ns()
            self._test_result(result)

    def test_under_partition(self):
        """Test that we succeed on network errors
        if we have a cached result."""
        self._setup_proxy(protocol='tcp')
        client = VClient()
        options = {
            'inject': True
        }

        result = self._query_ns()
        with client.with_behavior('error', **options):
            result = self._query_ns()
            self._test_result(result)

    def test_aws_transient(self):
        """Tests that we retry requests."""
        self._setup_proxy()
        client = VClient()

        with client.with_behavior('transient'):
            result = self._query_ns()
            self._test_result(result)

    def test_aws_5xx(self):
        """test that we succeed on 5xx errors if we have a cached
        result."""
        self._setup_proxy()
        client = VClient()
        options = {
            'inject': True
        }

        result = self._query_ns()
        with client.with_behavior('error', **options):
            result = self._query_ns()
            self._test_result(result)
