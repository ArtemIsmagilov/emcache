# MIT License
# Copyright (c) 2020-2024 Pau Freixes

cdef class AsciiOneLineParser:
    cdef object future
    cdef bytearray buffer_
