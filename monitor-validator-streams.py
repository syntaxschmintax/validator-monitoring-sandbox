import asyncio
import datetime
import json
import pprint
import sys
import threading
import time
try:
    import websockets
except ImportError:
    print('Could not import websockets module! Exiting script. (pip install websockets)')
    exit(1)

class XrplMonitorUtils(object):

    # Self-explanatory variables
    reconnect_count = 0
    max_retries = 5
    ledger_closed_threshold_seconds = 8.0
    consensus_phase_threshold_seconds = 6.0
    minimum_sms_gap_seconds = 300
    host_address, sms_destination, sms_source = '', '', ''
    twilio_credentials = {'auth_token': '', 'account_sid': ''}

    # Managed by connection (main) thread. Each response type receives a timestamp here when it arrives.
    type_timers = {}
    # Managed by monitoring thread. Every 5 seconds, the current time is compared against 'type_timers' and stored here.
    ms_since_last = {}

    # Dictionary to JSON-encode for initial websocket subscription
    subscribe_request = {"command": "subscribe", "streams": ["ledger", "consensus", "server"]}

    response_types = ['ledgerClosed', 'serverStatus', 'consensusPhase', 'response', 'totalRuntime']
    # Suppresses "serverStatus" from terminal output
    terminal_output_response_types = ['response', 'consensusPhase']

    def __init__(self, host_add, sms_dest, sms_src, twilio_auth=None, twilio_sid=None):
        self.host_address = host_add
        self.sms_destination = sms_dest
        self.sms_source = sms_src
        self.twilio_credentials['auth_token'] = twilio_auth
        self.twilio_credentials['account_sid'] = twilio_sid
        # Avoid some duplication by building both monitoring dictionaries when initialized.
        for cur_type in self.response_types:
            self.type_timers[cur_type] = 0.0 if cur_type != 'totalRuntime' else time.time()
            self.ms_since_last[cur_type] = 0.0

    @staticmethod
    def err_log_custom(err_message):
        sys.stderr.write("%s\t%s\n" % (str(datetime.datetime.now()).split('.')[0], err_message))
        sys.stderr.flush()

    def handle_response(self, response):
        response = json.loads(response)
        # Update 'last received at' time for current response type. Possibly print
        self.type_timers[response['type']] = time.time()
        if response['type'] in self.terminal_output_response_types:
            pprint.pprint(response)

    async def monitor_validator(self):
        # Connect to validator
        async with websockets.connect(self.host_address, ssl=True) as web_socket:
            # Subscribe to intended streams
            await web_socket.send(json.dumps(self.subscribe_request))
            # Log error + notify if restarting connection
            if self.reconnect_count > 0:
                msg_body = "Monitoring Connection Restarted"
                self.err_log_custom(msg_body)
                self.send_sms(msg_body)
            # Handle responses as they are received
            while True:
                self.handle_response(await web_socket.recv())

    # Function run by monitoring thread
    def monitor_response_times(self):
        sms_enabled = True
        latest_sms_time = time.time()
        while True:
            try:
                # Calculate response times every 5 seconds
                time.sleep(5)
                for res_type in self.ms_since_last:
                    # 'ms_since_last' is managed by the monitoring thread, 'type_timers' by the main thread
                    self.ms_since_last[res_type] = time.time() - self.type_timers[res_type]

                if sms_enabled:
                    # Check times and send SMS if necessary
                    send_msg = False
                    msg_body = ""

                    if float(self.ms_since_last['ledgerClosed']) >= self.ledger_closed_threshold_seconds:
                        send_msg = True
                        msg_body = "Warning: ledgerClosed time: %s" % self.ms_since_last['ledgerClosed']
                    if float(self.ms_since_last['consensusPhase']) >= self.consensus_phase_threshold_seconds:
                        send_msg = True
                        msg_body = "%s Warning: consensusPhase time: %s" % (msg_body, self.ms_since_last['consensusPhase'])

                    if send_msg:
                        # Consider SMS sent before sending. (Don't spam on SMS failures)
                        latest_sms_time = time.time()
                        sms_enabled = False
                        self.send_sms(msg_body)

                # Re-Enable SMS if enough time has passed since most-recent SMS
                if not sms_enabled:
                    time_diff = time.time() - latest_sms_time
                    if time_diff > self.minimum_sms_gap_seconds:
                        sms_enabled = True
                        print(" -- Re-Enabled SMS Warnings --")
                    else:
                        print(" -- SMS Warnings Disabled: %sms since last SMS --" % time_diff)

                # Descriptive output for humans
                pprint.pprint(self.ms_since_last)

            except Exception as ex:
                self.err_log_custom('Handled in-thread exception: %s' % repr(ex))
                pass

    # Runs the 'monitor_validator' function, restarting the connection if necessary and below 'max_retries'.
    def run_resilient_connection(self):
        try:
            asyncio.run(self.monitor_validator())
            self.reconnect_count = 0
        except Exception as ex:
            self.reconnect_count += 1
            ex_message = "Connection Closed! Attempting restart #%s in 10 seconds" % self.reconnect_count
            self.send_sms(ex_message)
            self.err_log_custom('--!-- %s --!-- Ex: %s' % (ex_message, repr(ex)))
            time.sleep(10)
            if self.reconnect_count <= self.max_retries:
                return self.run_resilient_connection()

    # Using the Twilio API, safely send an SMS. (Prevent any Twilio exception from crashing the script)
    def send_sms(self, msg_body):
        # Allow script to run without SMS capability
        if None not in [self.twilio_credentials['account_sid'], self.twilio_credentials['auth_token']]:
            try:
                client = Client(
                    self.twilio_credentials['account_sid'],
                    self.twilio_credentials['auth_token']
                )
                message = client.messages.create(body=msg_body, from_=self.sms_source, to=self.sms_destination)
                print("Sent SMS Warning (ID): ", message.sid)
                del client
            except Exception as _ex:
                self.err_log_custom('Handled exception (send_sms): %s' % repr(_ex))
                pass
        else:
            self.err_log_custom(" -- Warning: No SMS Credentials provided! -- ")


# Setup (Can run without adding SMS numbers + creds)
try:
    # Required for SMS capability (pip install twilio)
    from twilio.rest import Client
except ImportError:
    Client = None
    pass

host_address = "wss://s.altnet.rippletest.net"
# Replace with destination number and account number from Twilio. (Needs to start with a +)
sms_destination = '+15551114444'
sms_source = '+15552225555'
# Replace w/actual creds. Ideally, import from another file.
twilio_credentials = {
    'auth_token': None,
    'account_sid': None
}

# The Dude abides.
xrpl_utils = XrplMonitorUtils(
    host_address,
    sms_destination,
    sms_source,
    twilio_credentials['auth_token'],
    twilio_credentials['account_sid']
)

# Create monitoring thread. (Start monitoring websocket responses after a 5 second delay)
monitor_thread = threading.Timer(5, xrpl_utils.monitor_response_times)
# Ensure monitoring thread exits if main thread blows up entirely, then start it ;)
monitor_thread.daemon = True
monitor_thread.start()

# Run main thread validator connection
xrpl_utils.run_resilient_connection()
