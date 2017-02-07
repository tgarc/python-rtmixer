"""Reliable low-latency audio playback and recording."""
__version__ = '0.0.0'

import sounddevice as _sd
import time
import queue
from _rtmixer import ffi as _ffi, lib as _lib


class Mixer(_sd._StreamBase):
    """PortAudio stream for realtime mixing."""

    def __init__(self, channels, **kwargs):
        """Create a realtime mixer object.

        Takes the same keyword arguments as `sounddevice.Stream`, except
        *callback* and *dtype*.

        In contrast to `sounddevice.Stream`, the *channels* argument is
        not optional.

        Uses default values from `sounddevice.default`.

        """
        callback = _ffi.addressof(_lib, 'callback')

        # TODO: parameter for ring buffer size
        self._action_q = RingBuffer(_ffi.sizeof('struct action*'), 128)
        self._userdata = _ffi.new('struct state*', dict(
            input_channels=0,
            output_channels=channels,
            action_q=self._action_q._ptr,
        ))
        _sd._StreamBase.__init__(
            self, kind='output', wrap_callback=None, channels=channels,
            dtype='float32', callback=callback, userdata=self._userdata,
            **kwargs)

        self._actions = []

    def play_buffer(self, buffer, channels):
        """Send a buffer to the callback to be played back.

        After that, the *buffer* must not be written to anymore.

        """
        # TODO: drain result_q?
        channels, mapping = self._check_channels(channels, 'output')
        buffer = _ffi.from_buffer(buffer)
        _, samplesize = _sd._split(self.samplesize)
        action = _ffi.new('struct action*', dict(
            actiontype=_lib.PLAY_BUFFER,
            # Cast to float* to allow playing bytes objects:
            buffer=_ffi.cast('float*', buffer),
            total_frames=len(buffer) // channels // samplesize,
            channels=channels,
            mapping=mapping,
        ))
        # TODO: avoid code duplication with play_ringbuffer():
        if not self._action_q.write_available:
            raise RuntimeError('Action queue is full!')
        ret = self._action_q.write(_ffi.new('struct action**', action))
        assert ret == 1
        self._actions.append(action)  # TODO: Better way to keep alive?

    def play_ringbuffer(self, ringbuffer, channels=None):
        """Send a ring buffer to the callback to be played back.

        By default, the number of channels is obtained from the ring
        buffer's *elementsize*.

        """
        # TODO: drain result_q?
        _, samplesize = _sd._split(self.samplesize)
        if channels is None:
            channels = ringbuffer.elementsize // samplesize
        channels, mapping = self._check_channels(channels, 'output')
        if ringbuffer.elementsize != samplesize * channels:
            raise ValueError('Incompatible elementsize')
        action = _ffi.new('struct action*', dict(
            actiontype=_lib.PLAY_RINGBUFFER,
            ringbuffer=ringbuffer._ptr,
            channels=channels,
            mapping=mapping,
        ))
        if not self._action_q.write_available:
            raise RuntimeError('Action queue is full!')
        ret = self._action_q.write(_ffi.new('struct action**', action))
        assert ret == 1
        self._actions.append(action)  # TODO: Better way to keep alive?
        # TODO: block until playback has finished (optional)?
        # TODO: return something that allows stopping playback?

    def _check_channels(self, channels, kind):
        """Check if number of channels or mapping was given."""
        assert kind in ('input', 'output')
        try:
            channels, mapping = len(channels), channels
        except TypeError:
            mapping = tuple(range(1, channels + 1))
        max_channels = _sd._split(self.channels)[kind == 'output']
        if max(mapping) > max_channels:
            raise ValueError('Channel number too large')
        if min(mapping) < 1:
            raise ValueError('Channel numbers start with 1')
        return channels, mapping


class Recorder(_sd._StreamBase):
    """PortAudio stream for realtime recording."""


class MixerAndRecorder(Mixer, Recorder):
    """PortAudio stream for realtime mixing and recording."""


class RingBuffer(object):
    """Wrapper for PortAudio's ring buffer.

    See __init__().

    """
    class Full(Exception):
        pass

    class Empty(Exception):
        pass

    def __init__(self, elementsize, size):
        """Create an instance of PortAudio's ring buffer.

        Parameters
        ----------
        elementsize : int
            The size of a single data element in bytes.
        size : int
            The number of elements in the buffer (must be a power of 2).

        """
        self._ptr = _ffi.new('PaUtilRingBuffer*')
        self._data = _ffi.new('unsigned char[]', size * elementsize)
        res = _lib.PaUtil_InitializeRingBuffer(
            self._ptr, elementsize, size, self._data)
        if res != 0:
            assert res == -1
            raise ValueError('size must be a power of 2')
        assert self._ptr.bufferSize == size
        assert self._ptr.elementSizeBytes == elementsize

    def flush(self):
        """Reset buffer to empty.

        Should only be called when buffer is NOT being read or written.

        """
        _lib.PaUtil_FlushRingBuffer(self._ptr)

    @property
    def write_available(self):
        """Number of elements available in the ring buffer for writing."""
        return _lib.PaUtil_GetRingBufferWriteAvailable(self._ptr)

    @property
    def read_available(self):
        """Number of elements available in the ring buffer for reading."""
        return _lib.PaUtil_GetRingBufferReadAvailable(self._ptr)

    def write(self, data, size=-1):
        """Write data to the ring buffer.

        Parameters
        ----------
        data : CData pointer or buffer or bytes
            Data to write to the buffer.
        size : int, optional
            The number of elements to be written.

        Returns
        -------
        int
            The number of elements written.

        """
        try:
            data = _ffi.from_buffer(data)
        except TypeError:
            pass  # input is not a buffer
        if size < 0:
            size, rest = divmod(_ffi.sizeof(data), self._ptr.elementSizeBytes)
            if rest:
                raise ValueError('data size must be multiple of elementsize')
        return _lib.PaUtil_WriteRingBuffer(self._ptr, data, size)

    def read(self, data, size=-1):
        """Read data from the ring buffer.

        Parameters
        ----------
        data : CData pointer or buffer
            The memory where the data should be stored.
        size : int, optional
            The number of elements to be read.

        Returns
        -------
        int
            The number of elements read.

        """
        try:
            data = _ffi.from_buffer(data)
        except TypeError:
            pass  # input is not a buffer
        if size < 0:
            size, rest = divmod(len(data), self._ptr.elementSizeBytes)
            if rest:
                raise ValueError('data size must be multiple of elementsize')
        return _lib.PaUtil_ReadRingBuffer(self._ptr, data, size)

    def get(self, buff, size=-1, timeout=1):
        t1 = time.time()

        if size < 0:
            size, rest = divmod(len(buff), self._ptr.elementSizeBytes)
            if rest:
                raise ValueError('data size must be multiple of elementsize')

        while True:
            nframes = self.read_available
            if nframes < size:
                if timeout>0 and time.time()-t1 >= timeout: break
                _sd.sleep(10)
            else:
                nframes = self.read(buff, size=size)
                self.advance_read_index(nframes)
                return nframes

        raise queue.Empty("Timed out waiting for data.")

    def put(self, data, timeout=-1):
        t1 = time.time()

        size, rest = divmod(len(data), self._ptr.elementSizeBytes)
        if rest:
            raise ValueError('data size must be multiple of elementsize')

        while True:
            nframes = self.write_available
            if nframes < size:
                if timeout>0 and time.time()-t1 >= timeout: break
                _sd.sleep(10)
            else:
                nframes = self.write(data)
                self.advance_write_index(nframes)
                return nframes

        raise queue.Full("Timed out waiting to write data.")

    def get_write_buffers(self, size):
        """Get buffer(s) to which we can write data.

        Parameters
        ----------
        size : int
            The number of elements desired.

        Returns
        -------
        int
            The room available to be written or the given *size*,
            whichever is smaller.
        buffer
            The first buffer.
        buffer
            The second buffer.

        """
        ptr1 = _ffi.new('void**')
        ptr2 = _ffi.new('void**')
        size1 = _ffi.new('ring_buffer_size_t*')
        size2 = _ffi.new('ring_buffer_size_t*')
        return (_lib.PaUtil_GetRingBufferWriteRegions(
                    self._ptr, size, ptr1, size1, ptr2, size2),
                _ffi.buffer(ptr1[0], size1[0] * self.elementsize),
                _ffi.buffer(ptr2[0], size2[0] * self.elementsize))

    def advance_write_index(self, size):
        """Advance the write index to the next location to be written.

        Parameters
        ----------
        size : int
            The number of elements to advance.

        Returns
        -------
        int
            The new position.

        """
        return _lib.PaUtil_AdvanceRingBufferWriteIndex(self._ptr, size)

    def get_read_buffers(self, size):
        """Get buffer(s) from which we can read data.

        Parameters
        ----------
        size : int
            The number of elements desired.

        Returns
        -------
        int
            The number of elements available for reading.
        buffer
            The first buffer.
        buffer
            The second buffer.

        """
        ptr1 = _ffi.new('void**')
        ptr2 = _ffi.new('void**')
        size1 = _ffi.new('ring_buffer_size_t*')
        size2 = _ffi.new('ring_buffer_size_t*')
        return (_lib.PaUtil_GetRingBufferReadRegions(
                    self._ptr, size, ptr1, size1, ptr2, size2),
                _ffi.buffer(ptr1[0], size1[0] * self.elementsize),
                _ffi.buffer(ptr2[0], size2[0] * self.elementsize))

    def advance_read_index(self, size):
        """Advance the read index to the next location to be read.

        Parameters
        ----------
        size : int
            The number of elements to advance.

        Returns
        -------
        int
            The new position.

        """
        return _lib.PaUtil_AdvanceRingBufferReadIndex(self._ptr, size)

    @property
    def elementsize(self):
        """Element size in bytes."""
        return self._ptr.elementSizeBytes
