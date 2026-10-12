import sys
import threading
import time


class Tee:
	"""Writes to multiple files at once, so print() can go to both the console and log_file.
	Worker threads print into their own thread-local buffer instead, so concurrent trips don't interleave."""
	def __init__(self, *files):
		self.files = files
		self.local = threading.local()

	def _targets(self):
		buffer = getattr(self.local, "buffer", None)
		return (buffer,) if buffer is not None else self.files

	def write(self, data): [file.write(data) for file in self._targets()]
	def flush(self): [file.flush() for file in self._targets()]


def wait_for_output_dir(output_dir, retry_s=60):
	"""Block until output_dir (a network share) is reachable, so a dropped mount doesn't lose the results held in memory."""
	while True:
		try:
			if output_dir.is_dir():
				return
			error = "directory missing"
		except OSError as exc:
			error = exc
		# Straight to the console: the log file lives on the same share.
		print(f"Output dir unreachable ({error}): {output_dir}\n"
			  f"Results are kept in memory. Re-mount / re-authenticate the share; retrying in {retry_s}s...",
			  file=sys.__stdout__, flush=True)
		time.sleep(retry_s)
