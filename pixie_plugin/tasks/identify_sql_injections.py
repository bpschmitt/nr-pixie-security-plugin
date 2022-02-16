import json
import logging
import os
import re
import tempfile
from urllib import parse

import bleach
import pxapi

from pixie_plugin.config import get_config

logger = logging.getLogger(__name__)

SQL_INJECTION_RULE_DICT = {
    "script_tag": re.compile(r"(<|%3C)\s*script", flags=re.IGNORECASE),
    "comment_dashes": re.compile(r"--"),
    "comment_slash_star": re.compile(r"\/\*"),
    "semicolon": re.compile(r";"),
    "unmatched_quotes": re.compile(r"^([^']*'([^']*'[^']*')*[^']*')[^']*'[^']*$"),
    "always_true": re.compile(r"OR\s+(['\w]+)=\1", flags=re.IGNORECASE),
    "union": re.compile(r"UNION"),
    "char_casting": re.compile(r"CHR(\(|%28)", flags=re.IGNORECASE),
    "system_catalog_access": re.compile(r"FROM\s+pg_", flags=re.IGNORECASE),
}
# XSS_RULE_DICT = {
#     "script_tag": re.compile(r"(<|%3C)\s*script", flags=re.IGNORECASE),
# }
DANGER_WORDS = ["UPDATE", "DELETE", "INSERT", "SCRIPT", "DROP", "TRUNCATE"]
PXL_SCRIPT = """
import px

def add_source_dest_columns(df):
    ''' Add source and destination columns for the MySQL request.

    MySQL requests are traced server-side (trace_role==2), unless the server is
    outside of the cluster in which case the request is traced client-side (trace_role==1).

    When trace_role==2, the MySQL request source is the remote_addr column
    and destination is the pod column. When trace_role==1, the MySQL request
    source is the pod column and the destination is the remote_addr column.

    Input DataFrame must contain trace_role, upid, remote_addr columns.
    '''
    df.pod = df.ctx['pod']
    df.namespace = df.ctx['namespace']

    # If remote_addr is a pod, get its name. If not, use IP address.
    df.ra_pod = px.pod_id_to_pod_name(px.ip_to_pod_id(df.remote_addr))
    df.is_ra_pod = df.ra_pod != ''
    df.ra_name = px.select(df.is_ra_pod, df.ra_pod, df.remote_addr)

    df.is_server_tracing = df.trace_role == 2
    df.is_source_pod_type = px.select(df.is_server_tracing, df.is_ra_pod, True)
    df.is_dest_pod_type = px.select(df.is_server_tracing, True, df.is_ra_pod)

    # Set source and destination based on trace_role.
    df.source = px.select(df.is_server_tracing, df.ra_name, df.pod)
    df.destination = px.select(df.is_server_tracing, df.pod, df.ra_name)

    # Filter out messages with empty source / destination.
    df = df[df.source != '']
    df = df[df.destination != '']

    df = df.drop(['ra_pod', 'is_ra_pod', 'ra_name', 'is_server_tracing'])

    return df

fields = ['time_', 'remote_addr', 'remote_port', 'req_cmd',
            'req_body', 'resp_status', 'resp_body', 'latency', 'trace_role']
df = px.DataFrame(table='mysql_events', start_time = '-1m')[fields]
df = add_source_dest_columns(df)

px.display(df, 'mysql_table')

fields = ['time_', 'req_path', 'req_body']
df = px.DataFrame(table='http_events', start_time = '-1m')
px.display(df, 'http_table')

"""
BLACKLIST_WORDS = {"BEGIN", "COMMIT", "ROLLBACK"}
BLACKLIST_ENDPOINTS = [
    re.compile("^/readyz"),
    re.compile("^/px.api.*"),
    re.compile("^/$"),
    re.compile("^/health$"),
    re.compile("^/healthz$"),
    re.compile("^/latest/meta-data.*"),
]


def identify_sql_injections():
    settings = get_config()
    logger.info("Running identify sql injections task.")
    sql_queries, http_requests = _get_data_from_pixie(settings)
    filtered_sql_queries = _filter_data(sql_queries)
    sql_injections = _identify_sql_injections(filtered_sql_queries)
    filtered_http_requests = _filter_http_data(http_requests)
    # xss_events = _identify_xss(filtered_http_requests)
    # _submit_nr_events(settings, sql_injections + xss_events)
    _submit_nr_events(settings, sql_injections)


def _get_data_from_pixie(settings):
    client = pxapi.Client(token=settings["PIXIE_API_TOKEN"])
    conn = client.connect_to_cluster(settings["PIXIE_CLUSTER_ID"])

    sql_queries = []
    http_requests = []
    # Execute the PxL script.
    script = conn.prepare_script(PXL_SCRIPT)

    def _get_psql_data(row):
        sql_queries.append(row)

    def _get_http_req_data(row):
        http_requests.append(row)

    script.add_callback("http_table", _get_http_req_data)
    script.add_callback("mysql_table", _get_psql_data)
    script.run()

    return sql_queries, http_requests


def _filter_data(sql_queries):
    filtered_sql_queries = []

    for row in sql_queries:
        if row["req_body"] not in BLACKLIST_WORDS:
            filtered_sql_queries.append(row)
    return filtered_sql_queries


def _identify_sql_injections(filtered_sql_queries):
    """ Returns an array of dicts representing injection events. """
    sql_injections = []
    for query in filtered_sql_queries:
        for rule, regex in SQL_INJECTION_RULE_DICT.items():
            if regex.search(query["req_body"]):
                sql_injections.append(_create_injection_event(query, rule))
                logger.info(f"{query['req_body']} matched {rule} rule.")
    return sql_injections


def _identify_base_query(query_string):
    return query_string.split()[0]


def _identify_danger_words(query_string):
    words_found = [w for w in DANGER_WORDS if w in query_string.upper()]
    return ", ".join(words_found)


def _create_injection_event(query_row, rule):
    """
    Returns a dict representing a SQLInjection event with the given query string
    and rule.
    """
    return {
        "eventType": "SQLInjection",
        "query": query_row["req_body"],
        "remote_addr": query_row["remote_addr"],
        "remote_port": query_row["remote_port"],
        "source": query_row["source"],
        "destination": query_row["destination"],
        "baseQueryType": _identify_base_query(query_row["req_body"]),
        "dangerWords": _identify_danger_words(query_row["req_body"]),
        "rule": rule,
        "timestamp": query_row["time_"] / 10 ** 9,
    }


def _submit_nr_events(settings, events):
    """ Submit array of custom events to NR using the Event API. """
    events_json = json.dumps(events)
    # logger.info("Sending events to New Relic: %s" % events_json)
    nr_key = settings["NR_INSERT_KEY"]
    nr_account_id = settings["NR_ACCOUNT_ID"]

    temp_name = None
    with tempfile.NamedTemporaryFile(delete=False) as events_json_file:
        events_json_file.write(bytes(events_json, encoding="utf-8"))
        temp_name = events_json_file.name

    # Reference: https://docs.newrelic.com/docs/telemetry-data-platform/ingest-apis/
    # introduction-event-api/#submit-event
    os.system(  # nosec
        f'gzip -c {temp_name} | curl -X POST -H "Content-Type: application/json" '
        f'-H "X-Insert-Key: {nr_key}" -H "Content-Encoding: gzip" '
        f"https://insights-collector.newrelic.com/v1/accounts/{nr_account_id}"
        f"/events --data-binary @-"
    )
    os.remove(temp_name)


def _filter_http_data(http_requests):
    filtered_http_requests = []

    for row in http_requests:
        filter_row = False
        for filtered_endpoint in BLACKLIST_ENDPOINTS:
            if filtered_endpoint.search(row["req_path"]):
                filter_row = True
        if not filter_row:
            filtered_http_requests.append(row)
    return filtered_http_requests


# def _identify_xss(http_requests):
#     """ Returns an array of dicts representing xss events. """
#     xss_events = []
#     for request in http_requests:
#         parsed = parse.urlparse(request["req_path"])
#         query_params = parse.parse_qs(parsed.query)
#         for values in query_params.values():
#             for v in values:
#                 if bleach.clean(v) != v:
#                     xss_events.append(
#                         {
#                             "eventType": "XSSAttack",
#                             "path": request["req_path"],
#                             "body": request["req_body"],
#                             "rule": "xss",
#                             "timestamp": request["time_"] / 10 ** 9,
#                         }
#                     )
#                     logger.info(
#                         f"{request['req_path']} {request['req_body']} matched XSS rule."
#                     )
#     return xss_events