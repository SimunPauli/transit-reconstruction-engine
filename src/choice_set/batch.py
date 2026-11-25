import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src import otp
from .generate_alternatives import trip_choice_set, print_choice_set, CHOICE_SET_COLUMNS


class ThreadStdout(io.TextIOBase):
	"""Wraps a stream; a thread that set local.buffer prints into its buffer instead, so parallel trips don't interleave."""
	def __init__(self, real):
		self.real = real
		self.local = threading.local()

	def write(self, text):
		buffer = getattr(self.local, "buffer", None)
		return (self.real if buffer is None else buffer).write(text)

	def flush(self):
		self.real.flush()


def generate_choice_sets(trips, config, max_workers=1):
	"""
	Choice sets for trips (dicts of trip_choice_set's arguments except config), concatenated in trip order.
	Runs max_workers trips at a time, printing each trip's output in trip order. A trip that raises is skipped.
	"""
	stdout = ThreadStdout(sys.stdout)

	def choice_set_one(trip):
		"""One trip's choice set, buffering its prints. Returns (output, choice set or None)."""
		stdout.local.buffer = io.StringIO()
		otp.client.trace.id = f"trip_id={trip['trip_id']}"
		try:
			choice_set = trip_choice_set(**trip, config=config)
			print_choice_set(choice_set)
			print(f"Trip {trip['trip_id']}: {choice_set['alt_id'].max()} alternative(s) besides the chosen")
		except Exception as exc:
			print(f"Skipping trip {trip['trip_id']} due to unexpected error: {exc}")
			choice_set = None
		finally:
			output = stdout.local.buffer.getvalue()
			stdout.local.buffer = None
			otp.client.trace.id = None
		return output, choice_set

	# Trips are independent and I/O-bound on OTP, hence threads. map() yields in trip order.
	choice_sets = []
	with redirect_stdout(stdout), ThreadPoolExecutor(max_workers=max_workers) as executor:
		for output, choice_set in executor.map(choice_set_one, trips):
			print(output, end="")
			if choice_set is not None:
				choice_sets.append(choice_set)
	return pd.concat(choice_sets, ignore_index=True) if choice_sets else pd.DataFrame(columns=CHOICE_SET_COLUMNS)
