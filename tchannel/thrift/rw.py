# Copyright (c) 2015 Uber Technologies, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import absolute_import, print_function, unicode_literals

import sys
import types
from functools import partial

import thriftrw
from tornado import gen
from tornado.util import raise_exc_info

from tchannel.status import OK, FAILED
from tchannel.errors import OneWayNotSupportedError
from tchannel.errors import ValueExpectedError
from tchannel.response import Response, response_from_mixed
from tchannel.serializer.thrift import ThriftRWSerializer

from .module import ThriftRequest


def load(path, service=None, hostport=None, module_name=None):
    """Loads the Thrift file at the specified path.

    .. note::

        This functionality is experimental and subject to change. We expect to
        mark it as stable in a future version.

    The file is compiled in-memory and a Python module containing the result
    is returned. It may be used with ``TChannel.thrift``. For example,

    .. code-block:: python

        from tchannel import TChannel, thrift

        # Load our server's interface definition.
        donuts = thrift.load(path='donuts.thrift')

        # We need to specify a service name or hostport because this is a
        # downstream service we'll be calling.
        coffee = thrift.load(path='coffee.thrift', service='coffee')

        tchannel = TChannel('donuts')

        @tchannel.thrift.register(donuts.DonutsService)
        @tornado.gen.coroutine
        def submitOrder(request):
            args = request.body

            if args.coffee:
                yield tchannel.thrift(
                    coffee.CoffeeService.order(args.coffee)
                )

            # ...

    The returned module contains, one top-level type for each struct, enum,
    union, exeption, and service defined in the Thrift file. For each service,
    the corresponding class contains a classmethod for each function defined
    in that service that accepts the arguments for that function and returns a
    ``ThriftRequest`` capable of being sent via ``TChannel.thrift``.

    Note that the ``path`` accepted by ``load`` must be either an absolute
    path or a path relative to the *the current directory*. If you need to
    refer to Thrift files relative to the Python module in which ``load`` was
    called, use the ``__file__`` magic variable.

    .. code-block:: python

        # Given,
        #
        #   foo/
        #     myservice.thrift
        #     bar/
        #       x.py
        #
        # Inside foo/bar/x.py,

        path = os.path.join(
            os.path.dirname(__file__), '../myservice.thrift'
        )

    The returned value is a valid Python module. You can install the module by
    adding it to the ``sys.modules`` dictionary. This will allow importing
    items from this module directly. You can use the ``__name__`` magic
    variable to make the generated module a submodule of the current module.
    For example,

    .. code-block:: python

        # foo/bar.py

        import sys
        from tchannel import thrift

        donuts = = thrift.load('donuts.thrift')
        sys.modules[__name__ + '.donuts'] = donuts

    This installs the module generated for ``donuts.thrift`` as the module
    ``foo.bar.donuts``. Callers can then import items from that module
    directly. For example,

    .. code-block:: python

        # foo/baz.py

        from foo.bar.donuts import DonutsService, Order

        def baz(tchannel):
            return tchannel.thrift(
                DonutsService.submitOrder(Order(..))
            )

    :param str service:
        Name of the service that the Thrift file represents. This name will be
        used to route requests through Hyperbahn.
    :param str path:
        Path to the Thrift file. If this is a relative path, it must be
        relative to the current directory.
    :param str hostport:
        Clients can use this to specify the hostport at which the service can
        be found. If omitted, TChannel will route the requests through known
        peers. This value is ignored by servers.
    :param str module_name:
        Name used for the generated Python module. Defaults to the name of the
        Thrift file.
    """
    # TODO replace with more specific exceptions
    # assert service, 'service is required'
    # assert path, 'path is required'

    # Backwards compatibility for callers passing in service name as first arg.
    if not path.endswith('.thrift'):
        service, path = path, service

    module = thriftrw.load(path=path, name=module_name)
    return TChannelThriftModule(service, module, hostport)


class TChannelThriftModule(types.ModuleType):
    """Wraps the ``thriftrw``-generated module.

    Wraps service classes with ``Service`` and exposes everything else from
    the module as-is.
    """

    def __init__(self, service, module, hostport=None):
        """Initialize a TChannelThriftModule.

        :param str service:
            Name of the service this module represents. This name will be used
            for routing over Hyperbahn.
        :param module:
            Module generated by ``thriftrw`` for a Thrift file.
        :param str hostport:
            This may be specified if the caller is a client and wants all
            requests sent to a specific address.
        """

        self.service = service
        self.module = module
        self.hostport = hostport

        for service_cls in self.module.services:
            name = service_cls.service_spec.name
            setattr(self, name, Service(service_cls, self))

    def __getattr__(self, name):
        return getattr(self.module, name)

    def __str__(self):
        return 'TChannelThriftModule(%s, %s)' % (self.service, self.module)

    __repr__ = __str__


class Service(object):
    """Wraps service classes generated by thriftrw.

    Exposes all functions of the service.
    """

    def __init__(self, cls, module):
        self._module = module
        self._cls = cls
        self._spec = cls.service_spec

        for func_spec in self._spec.functions:
            setattr(self, func_spec.name, Function(func_spec, self))

    @property
    def name(self):
        """Name of the Thrift service this object represents."""
        return self._spec.name

    def __str__(self):
        return 'Service(%s)' % self.name

    __repr__ = __str__


class Function(object):
    """Wraps a ServiceFunction generated by thriftrw.

    Acts as a callable that will construct ThriftRequests.
    """

    __slots__ = (
        'spec', 'func', 'service', 'request_cls', 'response_cls'
    )

    def __init__(self, func_spec, service):
        self.spec = func_spec
        self.func = func_spec.surface
        self.service = service

        self.request_cls = self.func.request
        self.response_cls = self.func.response

    @property
    def endpoint(self):
        """Endpoint name for this function."""
        return '%s::%s' % (self.service.name, self.func.name)

    @property
    def oneway(self):
        """Whether this function is oneway."""
        return self.spec.oneway

    def __call__(self, *args, **kwargs):
        if self.oneway:
            raise OneWayNotSupportedError(
                'TChannel+Thrift does not currently support oneway '
                'procedures.'
            )

        if not (
            self.service._module.hostport or
            self.service._module.service
        ):
            raise ValueError(
                "No 'service' or 'hostport' provided to " +
                str(self)
            )

        module = self.service._module
        call_args = self.request_cls(*args, **kwargs)

        return ThriftRWRequest(
            module=module,
            service=module.service,
            endpoint=self.endpoint,
            result_type=self.response_cls,
            call_args=call_args,
            hostport=module.hostport,
        )

    def __str__(self):
        return 'Function(%s)' % self.endpoint

    __repr__ = __str__


def register(dispatcher, service, handler=None, method=None):
    """
    :param dispatcher:
        RequestDispatcher against which the new endpoint will be registered.
    :param Service service:
        Service object representing the service whose endpoint is being
        registered.
    :param handler:
        A function implementing the given Thrift function.
    :param method:
        If specified, name of the method being registered. Defaults to the
        name of the ``handler`` function.
    """

    def decorator(method, handler):
        if not method:
            method = handler.__name__

        function = getattr(service, method, None)
        assert function, (
            'Service "%s" does not define method "%s"' % (service.name, method)
        )
        assert not function.oneway

        handler = build_handler(function, handler)
        dispatcher.register(
            function.endpoint,
            handler,
            ThriftRWSerializer(service._module, function.request_cls),
            ThriftRWSerializer(service._module, function.response_cls),
        )
        return handler

    if handler is None:
        return partial(decorator, method)
    else:
        return decorator(method, handler)


def build_handler(function, handler):
    # response_cls is a class that represents the response union for this
    # function. It accepts one parameter for each exception defined on the
    # method and another parameter 'success' for the result of the call. The
    # success kwarg is absent if the function doesn't return anything.
    response_cls = function.response_cls
    response_spec = response_cls.type_spec

    @gen.coroutine
    def handle(request):
        # kwargs for this function's response_cls constructor
        response_kwargs = {}
        status = OK

        try:
            response = yield gen.maybe_future(handler(request))
        except Exception as e:
            response = Response()

            for exc_spec in response_spec.exception_specs:
                # Each exc_spec is a thriftrw.spec.FieldSpec. The spec
                # attribute on that is the TypeSpec for the Exception class
                # and the surface on the TypeSpec is the exception class.
                exc_cls = exc_spec.spec.surface
                if isinstance(e, exc_cls):
                    status = FAILED
                    response_kwargs[exc_spec.name] = e
                    break
            else:
                raise_exc_info(sys.exc_info())
        else:
            response = response_from_mixed(response)

            if response_spec.return_spec is not None:
                assert response.body is not None, (
                    'Expected a value to be returned for %s, '
                    'but recieved None - only void procedures can '
                    'return None.' % function.endpoint
                )
                response_kwargs['success'] = response.body

        response.status = status
        response.body = response_cls(**response_kwargs)
        raise gen.Return(response)

    handle.__name__ = function.spec.name

    return handle


class ThriftRWRequest(ThriftRequest):

    def __init__(self, module, **kwargs):
        kwargs['serializer'] = ThriftRWSerializer(
            module, kwargs['result_type']
        )
        super(ThriftRWRequest, self).__init__(**kwargs)

    def read_body(self, body):
        response_spec = self.result_type.type_spec

        for exc_spec in response_spec.exception_specs:
            exc = getattr(body, exc_spec.name)
            if exc is not None:
                raise exc

        # success - non-void
        if response_spec.return_spec is not None:
            if body.success is None:
                raise ValueExpectedError(
                    'Expected a value to be returned for %s, '
                    'but recieved None - only void procedures can '
                    'return None.' % self.endpoint
                )

            return body.success

        # success - void
        else:
            return None
