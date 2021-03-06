from math import ceil

from nmigen import Signal, Elaboratable, signed
from nmigen.build.res import ResourceError
from nmigen import Module
from nmigen.hdl.mem import Array

from luna.gateware.utils.cdc import synchronize
from luna.gateware.interface.spi import SPICommandInterface, SPIBus
from luna.gateware.memory import TransactionalizedFIFO

from FPGAG.resources import get_all_resources
from FPGAG.constants import (COMMAND_SIZE, WORD_SIZE, STATE, INSTRUCTIONS,
                             MEMWIDTH, COMMANDS, DEGREE, BIT_SHIFT,
                             MAX_TIME)


class SPIParser(Elaboratable):
    """ Parses and replies to commands over SPI

    The following commmands are possible
      status -- send back state of the peripheriral
      start  -- enable execution of gcode
      stop   -- halt execution of gcode
      write  -- write instruction to FIFO or report memory is full

    I/O signals:
        I: dispatcherror  -- error while processing stored command from spi
        O: execute        -- start processing gcode
        I/O: Spibus       -- spi bus connected to peripheral
        O: read_data      -- read data from transactionalizedfifo
        I: read_commit    -- finalize read transactionalizedfifo
        I: read_en        -- enable read transactionalizedfifo
        O: empty          -- transactionalizedfifo is empty
    """
    def __init__(self, platform=None, top=False):
        """
        platform  -- used to pass test platform
        """
        self.platform = platform
        self.top = top
        self.dispatcherror = Signal()
        self.execute = Signal()
        self.spi = SPIBus()
        self.read_data = Signal(MEMWIDTH)
        self.read_commit = Signal()
        self.read_en = Signal()
        self.empty = Signal()

    def elaborate(self, platform):
        m = Module()
        if platform and self.top:
            board_spi = platform.request("debug_spi")
            spi2 = synchronize(m, board_spi)
            m.d.comb += self.spi.connect(spi2)
        if self.platform:
            platform = self.platform
        spi = self.spi
        interface = SPICommandInterface(command_size=COMMAND_SIZE,
                                        word_size=WORD_SIZE)
        m.d.comb += interface.spi.connect(spi)
        m.submodules.interface = interface
        # Connect fifo
        fifo = TransactionalizedFIFO(width=MEMWIDTH,
                                     depth=platform.memdepth)
        if platform.name == 'Test':
            self.fifo = fifo
        m.submodules.fifo = fifo
        m.d.comb += [self.read_data.eq(fifo.read_data),
                     fifo.read_commit.eq(self.read_commit),
                     fifo.read_en.eq(self.read_en),
                     self.empty.eq(fifo.empty)]
        # set state
        state = Signal(COMMAND_SIZE)  # max is actually word_size
        m.d.sync += [state[STATE.FULL].eq(
                     fifo.space_available < ceil(platform.bytesinmove/4)),
                     state[STATE.DISPATCHERROR].eq(self.dispatcherror)]
        # Parser
        bytesreceived = Signal(range(platform.bytesinmove+1))
        with m.FSM(reset='RESET', name='parser'):
            with m.State('RESET'):
                m.d.sync += self.execute.eq(0)
                m.next = 'WAIT_COMMAND'
            with m.State('WAIT_COMMAND'):
                m.d.sync += [fifo.write_commit.eq(0)]
                with m.If(interface.command_ready):
                    with m.If(interface.command == COMMANDS.EMPTY):
                        m.next = 'WAIT_COMMAND'
                    with m.Elif(interface.command == COMMANDS.START):
                        m.next = 'WAIT_COMMAND'
                        m.d.sync += self.execute.eq(1)
                    with m.Elif(interface.command == COMMANDS.STOP):
                        m.next = 'WAIT_COMMAND'
                        m.d.sync += self.execute.eq(0)
                    with m.Elif(interface.command == COMMANDS.WRITE):
                        with m.If((state[STATE.FULL] == 0) |
                                  (bytesreceived != 0)):
                            m.next = 'WAIT_WORD'
                        with m.Else():
                            m.next = 'WAIT_COMMAND'
                            m.d.sync += [interface.word_to_send.eq(state)]
                    with m.Elif(interface.command == COMMANDS.STATUS):
                        m.d.sync += [interface.word_to_send.eq(state)]
                        m.next = 'WAIT_COMMAND'
            with m.State('WAIT_WORD'):
                with m.If(interface.word_complete):
                    m.d.sync += [bytesreceived.eq(bytesreceived+4),
                                 fifo.write_en.eq(1),
                                 fifo.write_data.eq(interface.word_received)]
                    m.next = 'WRITE'
            with m.State('WRITE'):
                m.d.sync += [fifo.write_en.eq(0)]
                m.next = 'WAIT_COMMAND'
                with m.If(bytesreceived == platform.bytesinmove):
                    m.d.sync += [bytesreceived.eq(0),
                                 fifo.write_commit.eq(1)]
        return m


class Polynomal(Elaboratable):
    """ Sets motor states using a polynomal algorithm

        A polynomal up to 3 order, e.g. c*x^3+b*x^2+a*x,
        is evaluated using the assumption that x starts at 0
        and y starts at 0. The polynomal determines the stepper
        position. The bitshift bit determines
        the position. In every tick the step can at most increase
        with one count.

        I/O signals:
        I: coeff          -- polynomal coefficients
        I: start          -- start signal
        O: busy           -- busy signal
        O: finished       -- finished signal
        O: total steps    -- total steps executed in move
        O: dir            -- direction; 1 is postive and 0 is negative
        O: step           -- step signal
    """
    def __init__(self, platform=None, motors=3,
                 bitshift=BIT_SHIFT, max_time=MAX_TIME):
        self.platform = platform
        self.order = DEGREE
        # change code for other orders
        assert self.order == 3
        self.motors = motors
        self.numb_coeff = motors*self.order
        self.bitshift = bitshift
        self.max_time = max_time
        self.max_steps = int(max_time/2)  # Nyquist
        # inputs
        self.coeff = Array(Signal(signed(32)) for _ in range(self.numb_coeff))
        self.start = Signal()
        # output
        self.busy = Signal()
        self.finished = Signal()
        self.totalsteps = Array(Signal(signed(self.max_steps.bit_length()+1))
                                for _ in range(motors))
        self.dir = Array(Signal() for _ in range(motors))
        self.step = Array(Signal() for _ in range(motors))

    def elaborate(self, platform):
        m = Module()
        # pos
        max_bits = (self.max_steps << self.bitshift).bit_length()
        cntrs = Array(Signal(signed(max_bits+1))
                      for _ in range(self.numb_coeff+self.motors))
        assert max_bits <= 64
        time = Signal(self.max_time.bit_length())
        if platform:
            steppers = [res for res in get_all_resources(platform, "stepper")]
        else:
            steppers = self.platform.steppers
            self.time = time
            self.cntrs = cntrs
        for idx, stepper in enumerate(steppers):
            m.d.comb += [stepper.step.eq(self.step[idx]),
                         stepper.dir.eq(self.dir[idx])]
        # steps
        for motor in range(self.motors):
            m.d.comb += [self.step[motor].eq(
                         cntrs[motor*self.order][self.bitshift]),
                         self.totalsteps[motor].eq(
                         cntrs[motor*self.order] >> (self.bitshift+1))]
        # directions
        counter_d = Array(Signal(signed(max_bits+1))
                          for _ in range(self.motors))
        for motor in range(self.motors):
            m.d.sync += counter_d[motor].eq(cntrs[motor*self.order])
            # negative case --> decreasing
            with m.If(counter_d[motor] > cntrs[motor*self.order]):
                m.d.sync += self.dir[motor].eq(0)
            # positive case --> increasing
            with m.Elif(counter_d[motor] < cntrs[motor*self.order]):
                m.d.sync += self.dir[motor].eq(1)
        with m.FSM(reset='RESET', name='polynomen'):
            with m.State('RESET'):
                m.next = 'WAIT_START'
                m.d.sync += [self.busy.eq(0),
                             self.finished.eq(0)]
            with m.State('WAIT_START'):
                with m.If(self.start):
                    m.d.sync += [self.busy.eq(1),
                                 self.finished.eq(0)]
                    m.next = 'RUNNING'
            with m.State('RUNNING'):
                with m.If(time < self.max_time):
                    m.d.sync += time.eq(time+1)
                    for motor in range(self.motors):
                        start = motor*self.order
                        op3 = 3*2*self.coeff[start+2] + cntrs[start+2]
                        op2 = (cntrs[start+2] + 2*self.coeff[start+1]
                               + cntrs[start+1])
                        op1 = (self.coeff[start+2] + self.coeff[start+1]
                               + self.coeff[start] + cntrs[start+2] +
                               cntrs[start+1] + cntrs[start])
                        m.d.sync += [cntrs[start+2].eq(op3),
                                     cntrs[start+1].eq(op2),
                                     cntrs[start].eq(op1)]
                with m.Else():
                    m.d.sync += [time.eq(0),
                                 self.busy.eq(0),
                                 self.finished.eq(1)]
                    m.next = 'WAIT_START'
        return m


class Dispatcher(Elaboratable):
    """ Dispatches instructions to right submodule

        Instructions are buffered in SRAM. This module checks the buffer
        and dispatches the instructions to the corresponding module.
        This is the top module"""
    def __init__(self, platform=None):
        """
        platform  -- used to pass test platform
        """
        self.platform = platform

    def elaborate(self, platform):
        m = Module()
        if platform:
            board_spi = platform.request("debug_spi")
            spi = synchronize(m, board_spi)
            steppers = [res for res in get_all_resources(platform, "steppers")]
            try:
                aux = platform.request("AUX")
            except ResourceError:
                aux = None
        else:
            platform = self.platform
            self.spi = SPIBus()
            spi = synchronize(m, self.spi)
            steppers = platform.steppers
            aux = platform.aux
            self.aux = aux
        # Connect Parser
        parser = SPIParser(self.platform)
        m.submodules.parser = parser
        m.d.comb += parser.spi.connect(spi)
        # Connect Polynomal Move module
        polynomal = Polynomal(self.platform)
        m.submodules.polynomal = polynomal
        # coeff for polynomal move
        coeffcnt = Signal(range(len(polynomal.coeff)))
        if platform.name == 'Test':
            self.parser = parser
            self.coeff = polynomal.coeff
        with m.FSM(reset='RESET', name='dispatcher'):
            with m.State('RESET'):
                m.next = 'WAIT_INSTRUCTION'
            with m.State('WAIT_INSTRUCTION'):
                m.d.sync += parser.read_commit.eq(0)
                with m.If((parser.empty == 0) & parser.execute):
                    m.d.sync += parser.read_en.eq(1)
                    m.next = 'PARSEHEAD'
            # check which instruction we r handling
            with m.State('PARSEHEAD'):
                with m.If(parser.read_data[:8] == INSTRUCTIONS.MOVE):
                    if aux is not None:
                        m.d.sync += aux.eq(parser.read_data[8:16])
                    m.d.sync += [parser.read_en.eq(0),
                                 coeffcnt.eq(0)]
                    m.next = 'MOVE_POLYNOMAL'
                with m.Else():
                    # NOTE: system never recovers user must reset
                    m.d.sync += parser.dispatcherror.eq(1)
            with m.State('MOVE_POLYNOMAL'):
                with m.If(parser.read_en == 0):
                    m.d.sync += parser.read_en.eq(1)
                with m.Elif(coeffcnt < len(polynomal.coeff)):
                    m.d.sync += [polynomal.coeff[coeffcnt].eq(
                                 parser.read_data),
                                 coeffcnt.eq(coeffcnt+1),
                                 parser.read_en.eq(0)]
                # signal there is a new instruction!!
                # ideally you can keep two instruction in memory
                with m.Else():
                    m.next = 'WAIT_INSTRUCTION'
                    m.d.sync += [parser.read_commit.eq(1),
                                 parser.read_en.eq(0)]
        return m


# Overview:
#  the hardware consists out of the following elements
#  -- SPI command interface
#  -- transactionalized FIFO
#  -- SPI parser (basically an extension of SPI command interface)
#  -- Dispatcher --> dispatches signals to actual hardware
#  -- Polynomal integrator --> determines position via integrating polynomen

# TODO:
#   -- motor should be updated with certain freq
#   -- connect modules
#   -- homing
#   -- you can now do too few steps in a move
