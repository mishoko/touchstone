// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Test} from "forge-std/Test.sol";
import {Pending} from "../../src/interfaces/ITouchstoneFactory.sol";
import {Tier} from "../../src/types/Tier.sol";
import {Obs} from "../../src/types/Obs.sol";
import {Holdings} from "../../src/types/Holdings.sol";
import {Bank, Cohort, RING} from "../../src/types/Bank.sol";
import {Tiers} from "../../script/Tiers.sol";
import {VaultHarness} from "./VaultHarness.sol";

/// @notice The scripted harness's hook: records `ackBreaches` as an event, in order with the vault's events.
contract AckRecorder {
    event AckBreaches(uint32 n);

    function ackBreaches(uint32 n) external {
        emit AckBreaches(n);
    }
}

/// @notice A stand-in factory whose `pending()` points the vault at `AckRecorder`; no PoolManager is used.
contract MockVaultFactory {
    Pending internal _p;

    constructor(address hook, Tier memory t) {
        _p.hook = hook;
        _p.key = PoolKey(Currency.wrap(address(1)), Currency.wrap(address(2)), 500, 10, IHooks(hook));
        _p.tier = t;
    }

    function POOL_MANAGER() external pure returns (address) {
        return address(0xFEED);
    }

    function pending() external view returns (Pending memory) {
        return _p;
    }

    /// @dev The harness's creation code arrives as calldata (embedding it would put this factory over EIP-170).
    function deploy(bytes memory code) external returns (VaultHarness h) {
        assembly {
            h := create(0, add(code, 0x20), mload(code))
        }
        require(address(h) != address(0), "deploy");
    }
}

/// @notice Base for tests on the scripted harness: a fresh harness per call and state builders.
abstract contract ScriptedVaultBase is Test {
    bytes32 internal constant T_CHECKPOINT =
        keccak256("Checkpoint(uint64,uint64,uint256,uint64,uint256,uint128,int24,int24)");
    bytes32 internal constant T_BREACH = keccak256("BreachProcessed(uint32,uint40,uint256)");
    bytes32 internal constant T_DEFERRED = keccak256("CheckpointDeferred()");
    bytes32 internal constant T_ACK = keccak256("AckBreaches(uint32)");

    function _newHarness() internal returns (VaultHarness h) {
        (Tier memory t,) = Tiers.testing();
        AckRecorder rec = new AckRecorder();
        h = new MockVaultFactory(address(rec), t).deploy(type(VaultHarness).creationCode);
    }

    /// @dev A placed vault at block 1200 (epoch 10, TEST tier): range [-600, 600), bond 1e21, S = 1e24, NORMAL.
    function _placedHarness() internal returns (VaultHarness h) {
        h = _newHarness();
        vm.roll(1200);
        h.setHoldings(Holdings(1e18, 1e18, 0, -600, 600));
        Bank memory b;
        b.bondShares = 1e21;
        b.dripBlocks = 120;
        b.releaseDelay = 480;
        h.setBank(b);
        h.setSupply(1e24);
        h.setScalars(2e18, 0, 0, 1150, 0, 10, 0, 0);
    }

    function _setCohort(VaultHarness h, uint32 e, uint128 shares, uint128 maturedPending) internal {
        Bank memory b = h.bankFull();
        b.cohorts[e % RING] = Cohort(e, shares);
        b.maturedPending = maturedPending;
        b.bondShares -= shares + maturedPending; // the bank's shares are vault-held, carved out of the bond
        h.setBank(b);
    }

    /// @dev A fresh, in-range, gap-free observation at the current block (refTick 0 inside [-600, 600)).
    function _obsIn() internal view returns (Obs memory o) {
        o.refTick = 0;
        o.refCachedAt = uint40(block.number - 1);
        o.refFresh = true;
        o.tauV = 60;
        o.gridIndex = uint32(block.number / 20);
        o.epoch = uint32(block.number / 120);
    }
}
