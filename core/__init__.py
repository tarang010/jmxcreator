# JMX Forge — core package
from .traffic_capture     import RecordingSession, deduplicate, build_curl_commands, CapturedRequest
from .transaction_grouper import group_into_transactions, Transaction
from .correlation_engine  import run_correlation, Correlation, CSVColumn
from .jmx_generator       import generate_jmx, generate_csv