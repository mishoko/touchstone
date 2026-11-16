// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice A scripted Uniswap v3-style price oracle for tests. It serves `observe`, `slot0`, `token0`, `token1`,
///         `fee` and `increaseObservationCardinalityNext` from a scripted tick path, and can be switched into failure
///         modes that a reference reader must survive.
///         Tick path: a list of segments `(start timestamp, tick)`; each tick holds from its start until the next
///         segment's start. History begins at the first segment; `observe` reverts "OLD" for any earlier target,
///         as a v3 pool does.
contract MockRefPool {
    enum Mode {
        Ok, // scripted cumulatives
        Old, // `observe` reverts "OLD" (observation too old)
        Revert, // `observe` reverts with empty data
        Short, // `observe` returns 64 bytes instead of the encoded arrays
        GasBomb, // `observe` burns all the gas it is given
        ReturnBomb // `observe` returns 100,000 bytes
    }

    address public immutable token0;
    address public immutable token1;
    uint24 public immutable fee;

    Mode public mode;
    /// @notice When non-zero, `observe` reverts cheaply if it is called with less gas than this (a reference whose read
    ///         fails early under low gas, rather than by running out of it: the model a gas guard must defeat).
    uint256 public gasFloor;
    uint16 public cardinality = 1000;
    uint16 public cardinalityNext = 1000;

    uint32[] internal _starts;
    int24[] internal _ticks;

    constructor(address t0, address t1, uint24 fee_, uint32 historyStart, int24 tick0) {
        token0 = t0;
        token1 = t1;
        fee = fee_;
        _starts.push(historyStart);
        _ticks.push(tick0);
    }

    // ---------------------------------------------------------------- scripting

    function setMode(Mode m) external {
        mode = m;
    }

    function setGasFloor(uint256 g) external {
        gasFloor = g;
    }

    function setCardinality(uint16 c) external {
        cardinality = c;
        cardinalityNext = c;
    }

    /// @notice The tick changes to `tick` from now on (replaces a segment that started in this same second).
    function setTick(int24 tick) external {
        uint256 n = _starts.length;
        if (_starts[n - 1] == uint32(block.timestamp)) {
            _ticks[n - 1] = tick;
        } else {
            require(uint32(block.timestamp) > _starts[n - 1], "MockRefPool: time");
            _starts.push(uint32(block.timestamp));
            _ticks.push(tick);
        }
    }

    /// @notice Replaces the whole path. `starts` must be strictly increasing.
    function setPath(uint32[] calldata starts, int24[] calldata ticks) external {
        require(starts.length == ticks.length && starts.length > 0, "MockRefPool: path");
        for (uint256 i = 1; i < starts.length; i++) {
            require(starts[i] > starts[i - 1], "MockRefPool: order");
        }
        _starts = starts;
        _ticks = ticks;
    }

    function tickAt(uint32 t) public view returns (int24 tick) {
        tick = _ticks[0];
        for (uint256 i = 1; i < _starts.length && _starts[i] <= t; i++) {
            tick = _ticks[i];
        }
    }

    function tickCumulativeAt(uint32 t) public view returns (int56 c) {
        require(t >= _starts[0], "OLD");
        uint256 n = _starts.length;
        for (uint256 i = 0; i < n && _starts[i] < t; i++) {
            uint32 end = i + 1 < n && _starts[i + 1] < t ? _starts[i + 1] : t;
            c += int56(_ticks[i]) * int56(uint56(end - _starts[i]));
        }
    }

    // ---------------------------------------------------------------- v3 oracle surface

    function observe(uint32[] calldata secondsAgos)
        external
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulativeX128s)
    {
        if (gasleft() < gasFloor) revert();
        Mode m = mode;
        if (m == Mode.Old) revert("OLD");
        if (m == Mode.Revert) revert();
        if (m == Mode.Short) {
            assembly {
                mstore(0, 0x40)
                mstore(0x20, 1)
                return(0, 0x40)
            }
        }
        if (m == Mode.GasBomb) {
            uint256 x;
            while (true) {
                x = uint256(keccak256(abi.encode(x)));
            }
        }
        if (m == Mode.ReturnBomb) {
            assembly {
                return(0, 100000)
            }
        }
        tickCumulatives = new int56[](secondsAgos.length);
        secondsPerLiquidityCumulativeX128s = new uint160[](secondsAgos.length);
        for (uint256 i = 0; i < secondsAgos.length; i++) {
            tickCumulatives[i] = tickCumulativeAt(uint32(block.timestamp) - secondsAgos[i]);
        }
    }

    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24 tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8 feeProtocol,
            bool unlocked
        )
    {
        tick = tickAt(uint32(block.timestamp));
        sqrtPriceX96 = TickMath.getSqrtPriceAtTick(tick);
        observationIndex = 0;
        observationCardinality = cardinality;
        observationCardinalityNext = cardinalityNext;
        feeProtocol = 0;
        unlocked = true;
    }

    function increaseObservationCardinalityNext(uint16 next) external {
        if (next > cardinalityNext) cardinalityNext = next;
    }
}
