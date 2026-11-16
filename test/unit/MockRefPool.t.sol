// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {MockRefPool} from "../utils/MockRefPool.sol";

/// @notice A reference read of the kind the hook performs: a `staticcall` of `observe([window, 0])` with a gas cap and
///         a fixed-size return buffer; ok iff the call succeeds and returns exactly two cumulatives; the tick is the
///         time-weighted mean rounded toward negative infinity. It never reverts.
contract RefReader {
    uint256 internal constant RET = 256; // abi.encode(int56[2], uint160[2])

    function read(address pool, uint32 window, uint256 gasCap) external view returns (bool ok, int24 tick) {
        uint32[] memory ago = new uint32[](2);
        ago[0] = window;
        bytes memory cd = abi.encodeWithSignature("observe(uint32[])", ago);
        bool success;
        uint256 rds;
        int256 c0;
        int256 c1;
        uint256 off;
        uint256 len;
        assembly {
            let out := mload(0x40)
            mstore(0x40, add(out, RET))
            success := staticcall(gasCap, pool, add(cd, 0x20), mload(cd), out, RET)
            rds := returndatasize()
            off := mload(out)
            len := mload(add(out, 0x40))
            c0 := signextend(6, mload(add(out, 0x60)))
            c1 := signextend(6, mload(add(out, 0x80)))
        }
        if (!success || rds != RET || off != 0x40 || len != 2) return (false, 0);
        int256 d = c1 - c0;
        int256 w = int256(uint256(window));
        int256 t = d / w;
        if (d < 0 && d % w != 0) t--;
        if (t < TickMath.MIN_TICK) t = TickMath.MIN_TICK;
        if (t > TickMath.MAX_TICK) t = TickMath.MAX_TICK;
        return (true, int24(t));
    }
}

contract MockRefPoolTest is Test {
    uint32 constant WINDOW = 600;
    uint256 constant GAS_CAP = 200_000;
    MockRefPool pool;
    RefReader reader;

    function setUp() public {
        vm.warp(1_000_000);
        pool = new MockRefPool(address(1), address(2), 500, uint32(block.timestamp - 7200), 100);
        reader = new RefReader();
    }

    function test_mockRef_ok_returnsScriptedTwap() public {
        (bool ok, int24 tick) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertTrue(ok);
        assertEq(tick, 100);
        // half the window at 100, half at -101: mean -0.5 rounds toward -inf to -1
        vm.warp(block.timestamp + 1);
        pool.setTick(-101);
        vm.warp(block.timestamp + 300);
        (ok, tick) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertTrue(ok);
        assertEq(tick, -1);
        (, int24 spot,, uint16 card,,,) = pool.slot0();
        assertEq(spot, -101);
        assertEq(card, 1000);
    }

    function test_mockRef_old_failsSoft() public {
        pool.setMode(MockRefPool.Mode.Old);
        (bool ok,) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertFalse(ok);
        // a window reaching before the scripted history also reverts "OLD" in mode Ok
        pool.setMode(MockRefPool.Mode.Ok);
        (ok,) = reader.read(address(pool), 7201, GAS_CAP);
        assertFalse(ok);
        vm.expectRevert(bytes("OLD"));
        uint32[] memory ago = new uint32[](2);
        ago[0] = 7201;
        pool.observe(ago);
    }

    function test_mockRef_revert_failsSoft() public {
        pool.setMode(MockRefPool.Mode.Revert);
        (bool ok,) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertFalse(ok);
    }

    function test_mockRef_shortReturn_failsSoft() public {
        pool.setMode(MockRefPool.Mode.Short);
        (bool ok,) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertFalse(ok);
    }

    function test_mockRef_gasBomb_failsSoftWithinCap() public {
        pool.setMode(MockRefPool.Mode.GasBomb);
        uint256 g = gasleft();
        (bool ok,) = reader.read(address(pool), WINDOW, GAS_CAP);
        uint256 used = g - gasleft();
        assertFalse(ok);
        assertLt(used, GAS_CAP + 30_000);
        assertGt(used, GAS_CAP);
    }

    function test_mockRef_returnBomb_failsSoft() public {
        pool.setMode(MockRefPool.Mode.ReturnBomb);
        uint256 g = gasleft();
        (bool ok,) = reader.read(address(pool), WINDOW, GAS_CAP);
        assertFalse(ok);
        assertLt(g - gasleft(), GAS_CAP + 30_000);
    }
}
