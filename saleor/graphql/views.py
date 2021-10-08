import fnmatch
import hashlib
import json
import logging
import traceback
from typing import Any, Dict, List, Optional, Tuple, Union

import opentracing
import opentracing.tags
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.backends.postgresql.base import DatabaseWrapper
from django.http import HttpRequest, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.functional import SimpleLazyObject
from django.views.generic import View
# from graphene_django.settings import graphene_settings
# from graphene_django.views import instantiate_middleware
from graphql import DocumentNode
from graphql.error import GraphQLError, GraphQLSyntaxError
from graphql.error import format_error as format_graphql_error
from graphql.execution import ExecutionResult
from jwt.exceptions import PyJWTError

from .. import __version__ as saleor_version
from ..core.exceptions import PermissionDenied, ReadOnlyException
from ..core.utils import is_valid_ipv4, is_valid_ipv6
from .utils import query_fingerprint

API_PATH = SimpleLazyObject(lambda: reverse("api"))
INT_ERROR_MSG = "Int cannot represent non 32-bit signed integer value"

unhandled_errors_logger = logging.getLogger("saleor.graphql.errors.unhandled")
handled_errors_logger = logging.getLogger("saleor.graphql.errors.handled")


def tracing_wrapper(execute, sql, params, many, context):
    conn: DatabaseWrapper = context["connection"]
    operation = f"{conn.alias} {conn.display_name}"
    with opentracing.global_tracer().start_active_span(operation) as scope:
        span = scope.span
        span.set_tag(opentracing.tags.COMPONENT, "db")
        span.set_tag(opentracing.tags.DATABASE_STATEMENT, sql)
        span.set_tag(opentracing.tags.DATABASE_TYPE, conn.display_name)
        span.set_tag(opentracing.tags.PEER_HOSTNAME, conn.settings_dict.get("HOST"))
        span.set_tag(opentracing.tags.PEER_PORT, conn.settings_dict.get("PORT"))
        span.set_tag("service.name", "postgres")
        span.set_tag("span.type", "sql")
        return execute(sql, params, many, context)


class GraphQLView(View):
    # This class is our implementation of `graphene_django.views.GraphQLView`,
    # which was extended to support the following features:
    # - Playground as default the API explorer (see
    # https://github.com/prisma/graphql-playground)
    # - file upload (https://github.com/lmcgartland/graphene-file-upload)
    # - query batching
    # - CORS

    schema = None
    executor = None
    backend = None
    middleware = None
    root_value = None

    HANDLED_EXCEPTIONS = (GraphQLError, PyJWTError, ReadOnlyException, PermissionDenied)

    def __init__(
        self, schema=None, executor=None, middleware=None, root_value=None, backend=None
    ):
        super().__init__()
        if schema is None:
            schema = graphene_settings.SCHEMA
        if backend is None:
            backend = get_default_backend()
        if middleware is None:
            middleware = graphene_settings.MIDDLEWARE
        self.schema = self.schema or schema
        if middleware is not None:
            self.middleware = list(instantiate_middleware(middleware))
        self.executor = executor
        self.root_value = root_value
        self.backend = backend

    def dispatch(self, request, *args, **kwargs):
        # Handle options method the GraphQlView restricts it.
        if request.method == "GET":
            if settings.PLAYGROUND_ENABLED:
                return self.render_playground(request)
            return HttpResponseNotAllowed(["OPTIONS", "POST"])
        if request.method == "OPTIONS":
            response = self.options(request, *args, **kwargs)
        elif request.method == "POST":
            response = self.handle_query(request)
        else:
            return HttpResponseNotAllowed(["GET", "OPTIONS", "POST"])
        # Add access control headers
        if "HTTP_ORIGIN" in request.META:
            for origin in settings.ALLOWED_GRAPHQL_ORIGINS:
                if fnmatch.fnmatchcase(request.META["HTTP_ORIGIN"], origin):
                    response["Access-Control-Allow-Origin"] = request.META[
                        "HTTP_ORIGIN"
                    ]
                    response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
                    response["Access-Control-Allow-Headers"] = (
                        "Origin, Content-Type, Accept, Authorization, "
                        "Authorization-Bearer"
                    )
                    response["Access-Control-Allow-Credentials"] = "true"
                    break
        return response

    def render_playground(self, request):
        return render(
            request,
            "graphql/playground.html",
            {"api_url": request.build_absolute_uri(str(API_PATH))},
        )

    def _handle_query(self, request: HttpRequest) -> JsonResponse:
        try:
            data = self.parse_body(request)
        except ValueError:
            return JsonResponse(
                data={"errors": [self.format_error("Unable to parse query.")]},
                status=400,
            )

        if isinstance(data, list):
            responses = [self.get_response(request, entry) for entry in data]
            result: Union[list, Optional[dict]] = [
                response for response, code in responses
            ]
            status_code = max((code for response, code in responses), default=200)
        else:
            result, status_code = self.get_response(request, data)
        return JsonResponse(data=result, status=status_code, safe=False)

    def handle_query(self, request: HttpRequest) -> JsonResponse:
        tracer = opentracing.global_tracer()

        # Disable extending spans from header due to:
        # https://github.com/DataDog/dd-trace-py/issues/2030

        # span_context = tracer.extract(
        #     format=Format.HTTP_HEADERS, carrier=dict(request.headers)
        # )
        # We should:
        # Add `from opentracing.propagation import Format` to imports
        # Add `child_of=span_ontext` to `start_active_span`
        with tracer.start_active_span("http") as scope:
            span = scope.span
            span.set_tag(opentracing.tags.COMPONENT, "http")
            span.set_tag(opentracing.tags.HTTP_METHOD, request.method)
            span.set_tag(
                opentracing.tags.HTTP_URL,
                request.build_absolute_uri(request.get_full_path()),
            )
            span.set_tag("http.useragent", request.META.get("HTTP_USER_AGENT", ""))
            span.set_tag("span.type", "web")

            request_ips = request.META.get(settings.REAL_IP_ENVIRON, "")
            for ip in request_ips.split(","):
                if is_valid_ipv4(ip):
                    span.set_tag(opentracing.tags.PEER_HOST_IPV4, ip)
                elif is_valid_ipv6(ip):
                    span.set_tag(opentracing.tags.PEER_HOST_IPV6, ip)
                else:
                    continue
                span.set_tag("http.client_ip", ip)
                span.set_tag("http.client_ip_originated_from", settings.REAL_IP_ENVIRON)
                break

            response = self._handle_query(request)
            span.set_tag(opentracing.tags.HTTP_STATUS_CODE, response.status_code)

            # RFC2616: Content-Length is defined in bytes,
            # we can calculate the RAW UTF-8 size using the length of
            # response.content of type 'bytes'
            span.set_tag("http.content_length", len(response.content))

            return response

    def get_response(
        self, request: HttpRequest, data: dict
    ) -> Tuple[Optional[Dict[str, List[Any]]], int]:
        execution_result = self.execute_graphql_request(request, data)
        status_code = 200
        if execution_result:
            response = {}
            if execution_result.errors:
                response["errors"] = [
                    self.format_error(e) for e in execution_result.errors
                ]
            if execution_result.invalid:
                status_code = 400
            else:
                response["data"] = execution_result.data
            result: Optional[Dict[str, List[Any]]] = response
        else:
            result = None

        return result, status_code

    def get_root_value(self):
        return self.root_value

    def parse_query(
        self, query: str
    ) -> Tuple[Optional[DocumentNode], Optional[ExecutionResult]]:
        """Attempt to parse a query (mandatory) to a gql document object.

        If no query was given or query is not a string, it returns an error.
        If the query is invalid, it returns an error as well.
        Otherwise, it returns the parsed gql document.
        """
        if not query or not isinstance(query, str):
            return (
                None,
                ExecutionResult(
                    errors=[ValueError("Must provide a query string.")], invalid=True
                ),
            )

        # Attempt to parse the query, if it fails, return the error
        try:
            return (
                self.backend.document_from_string(self.schema, query),  # type: ignore
                None,
            )
        except (ValueError, GraphQLSyntaxError) as e:
            return None, ExecutionResult(errors=[e], invalid=True)

    def check_if_query_contains_only_schema(self, document: DocumentNode):
        query_with_schema = False
        for definition in document.document_ast.definitions:
            selections = definition.selection_set.selections
            selection_count = len(selections)
            for selection in selections:
                selection_name = str(selection.name.value)
                if selection_name == "__schema":
                    query_with_schema = True
                    if selection_count > 1:
                        msg = "`__schema` must be fetched in separate query"
                        raise GraphQLError(msg)
        return query_with_schema

    def execute_graphql_request(self, request: HttpRequest, data: dict):
        with opentracing.global_tracer().start_active_span("graphql_query") as scope:
            span = scope.span
            span.set_tag(opentracing.tags.COMPONENT, "graphql")

            query, variables, operation_name = self.get_graphql_params(request, data)

            document, error = self.parse_query(query)
            if error:
                return error

            if document is not None:
                raw_query_string = document.document_string
                span.set_tag("graphql.query", raw_query_string)
                span.set_tag("graphql.query_fingerprint", query_fingerprint(document))
                try:
                    query_contains_schema = self.check_if_query_contains_only_schema(
                        document
                    )
                except GraphQLError as e:
                    return ExecutionResult(errors=[e], invalid=True)

            extra_options: Dict[str, Optional[Any]] = {}

            if self.executor:
                # We only include it optionally since
                # executor is not a valid argument in all backends
                extra_options["executor"] = self.executor
            try:
                with connection.execute_wrapper(tracing_wrapper):
                    response = None
                    should_use_cache_for_scheme = query_contains_schema & (
                        not settings.DEBUG
                    )
                    if should_use_cache_for_scheme:
                        key = generate_cache_key(raw_query_string)
                        response = cache.get(key)

                    if not response:
                        response = document.execute(  # type: ignore
                            root=self.get_root_value(),
                            variables=variables,
                            operation_name=operation_name,
                            context=request,
                            middleware=self.middleware,
                            **extra_options,
                        )
                        if should_use_cache_for_scheme:
                            cache.set(key, response)
                    return response
            except Exception as e:
                span.set_tag(opentracing.tags.ERROR, True)
                # In the graphql-core version that we are using,
                # the Exception is raised for too big integers value.
                # As it's a validation error we want to raise GraphQLError instead.
                if str(e).startswith(INT_ERROR_MSG) or isinstance(e, ValueError):
                    e = GraphQLError(str(e))
                return ExecutionResult(errors=[e], invalid=True)

    @staticmethod
    def parse_body(request: HttpRequest):
        content_type = request.content_type
        if content_type == "application/graphql":
            return {"query": request.body.decode("utf-8")}
        if content_type == "application/json":
            body = request.body.decode("utf-8")
            return json.loads(body)
        if content_type in ["application/x-www-form-urlencoded", "multipart/form-data"]:
            return request.POST
        return {}

    @staticmethod
    def get_graphql_params(request: HttpRequest, data: dict):
        query = data.get("query")
        variables = data.get("variables")
        operation_name = data.get("operationName")
        if operation_name == "null":
            operation_name = None

        if request.content_type == "multipart/form-data":
            operations = json.loads(data.get("operations", "{}"))
            files_map = json.loads(data.get("map", "{}"))
            for file_key in files_map:
                # file key is which file it is in the form-data
                file_instances = files_map[file_key]
                for file_instance in file_instances:
                    obj_set(operations, file_instance, file_key, False)
            query = operations.get("query")
            variables = operations.get("variables")
        return query, variables, operation_name

    @classmethod
    def format_error(cls, error):
        if isinstance(error, GraphQLError):
            result = format_graphql_error(error)
        else:
            result = {"message": str(error)}

        exc = error
        while isinstance(exc, GraphQLError) and hasattr(exc, "original_error"):
            exc = exc.original_error
        if isinstance(exc, AssertionError):
            exc = GraphQLError(str(exc))
        if isinstance(exc, cls.HANDLED_EXCEPTIONS):
            handled_errors_logger.info("A query had an error", exc_info=exc)
        else:
            unhandled_errors_logger.error("A query failed unexpectedly", exc_info=exc)

        result["extensions"] = {"exception": {"code": type(exc).__name__}}
        if settings.DEBUG:
            lines = []

            if isinstance(exc, BaseException):
                for line in traceback.format_exception(
                    type(exc), exc, exc.__traceback__
                ):
                    lines.extend(line.rstrip().splitlines())
            result["extensions"]["exception"]["stacktrace"] = lines
        return result


def get_key(key):
    try:
        int_key = int(key)
    except (TypeError, ValueError):
        return key
    else:
        return int_key


def get_shallow_property(obj, prop):
    if isinstance(prop, int):
        return obj[prop]
    try:
        return obj.get(prop)
    except AttributeError:
        return None


def obj_set(obj, path, value, do_not_replace):
    if isinstance(path, int):
        path = [path]
    if not path:
        return obj
    if isinstance(path, str):
        new_path = [get_key(part) for part in path.split(".")]
        return obj_set(obj, new_path, value, do_not_replace)

    current_path = path[0]
    current_value = get_shallow_property(obj, current_path)

    if len(path) == 1:
        if current_value is None or not do_not_replace:
            obj[current_path] = value

    if current_value is None:
        try:
            if isinstance(path[1], int):
                obj[current_path] = []
            else:
                obj[current_path] = {}
        except IndexError:
            pass
    return obj_set(obj[current_path], path[1:], value, do_not_replace)


def generate_cache_key(raw_query: str) -> str:
    hashed_query = hashlib.sha256(str(raw_query).encode("utf-8")).hexdigest()
    return f"{saleor_version}-{hashed_query}"
