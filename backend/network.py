"""进程内强制直连；不修改用户的系统代理、证书或全局设置。"""
from __future__ import annotations

import os
import urllib.request
from functools import wraps

_configured = False

def configure_direct_network():
    global _configured
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):
            os.environ.pop(key, None)
    os.environ['NO_PROXY'] = '*'
    os.environ['no_proxy'] = '*'
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    if _configured:
        return

    # Also cover SDK-created openers with an explicit ProxyHandler.
    urllib.request.ProxyHandler.proxy_open = lambda self,req,proxy,type: None

    import requests
    original_init = requests.Session.__init__
    original_send = requests.Session.send

    @wraps(original_init)
    def direct_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.trust_env = False
        self.proxies = {}

    @wraps(original_send)
    def direct_send(self, request, **kwargs):
        self.trust_env = False
        self.proxies = {}
        kwargs['proxies'] = {}
        kwargs.setdefault('timeout', 12)
        return original_send(self, request, **kwargs)

    requests.Session.__init__ = direct_init
    requests.Session.send = direct_send

    # AKShare 的部分接口使用 curl_cffi；libcurl 也必须明确关闭代理。
    from curl_cffi import CurlOpt
    from curl_cffi.requests.session import BaseSession, Session, AsyncSession
    original_curl_init = BaseSession.__init__

    @wraps(original_curl_init)
    def direct_curl_init(self, **kwargs):
        kwargs.update(trust_env=False, proxies={}, proxy=None)
        options = dict(kwargs.get('curl_options') or {})
        options.update({CurlOpt.PROXY: '', CurlOpt.NOPROXY: '*'})
        kwargs['curl_options'] = options
        original_curl_init(self, **kwargs)

    BaseSession.__init__ = direct_curl_init
    original_curl_request=Session.request
    original_async_request=AsyncSession.request

    def force_curl_direct(session):
        session.trust_env=False
        session.proxies={}
        options=dict(session.curl_options or {})
        options.update({CurlOpt.PROXY:'',CurlOpt.NOPROXY:'*'})
        session.curl_options=options

    @wraps(original_curl_request)
    def direct_curl_request(self,*args,**kwargs):
        force_curl_direct(self)
        return original_curl_request(self,*args,**kwargs)

    @wraps(original_async_request)
    async def direct_async_request(self,*args,**kwargs):
        force_curl_direct(self)
        return await original_async_request(self,*args,**kwargs)

    Session.request=direct_curl_request
    AsyncSession.request=direct_async_request
    _configured = True
