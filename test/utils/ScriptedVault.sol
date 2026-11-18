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
import {TouchstoneVaultExtension} from "../../src/TouchstoneVaultExtension.sol";
import {VaultCommon} from "../../src/TouchstoneVaultBase.sol";

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

    /// @dev The harness's and the extension's creation codes arrive as calldata (embedding them would put this
    ///      factory over EIP-170). Both addresses are computed first and handed over in `pending`, as the factory does.
    function deploy(bytes memory code, bytes memory extCode) external returns (VaultHarness h) {
        return VaultHarness(deployWith(code, extCode, 0));
    }

    /// @notice A deployment with one defect for the vault constructor's consistency checks (0: none): 1 the extension
    ///         is built for another vault (`VAULT()` mismatch); 2 the extension is built under another tier
    ///         (`CONFIG_HASH` mismatch); 3 `pending.extension` points at an address with no code.
    function deployWith(bytes memory code, bytes memory extCode, uint8 defect) public returns (address v) {
        bytes32 sv = keccak256(abi.encode(msg.sender, ++_n));
        bytes32 se = keccak256(abi.encode(sv, "ext"));
        address vaultAddr = _addr(sv, code);
        _p.vault = defect == 1 ? address(0xBAD) : vaultAddr;
        _p.extension = defect == 3 ? address(0xC0DE) : _addr(se, extCode);
        Tier memory t = _p.tier;
        if (defect == 2) _p.tier.CURE_BLOCKS += 1;
        address x = _create2(extCode, se);
        (_p.vault, _p.tier) = (vaultAddr, t);
        if (defect == 3) _p.extension = address(0xC0DE);
        else _p.extension = x;
        v = _create2(code, sv);
    }

    uint256 internal _n;

    function _addr(bytes32 salt, bytes memory code) internal view returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), address(this), salt, keccak256(code))))));
    }

    function _create2(bytes memory code, bytes32 salt) internal returns (address a) {
        assembly {
            a := create2(0, add(code, 0x20), mload(code), salt)
            if iszero(a) {
                let p := mload(0x40)
                returndatacopy(p, 0, returndatasize())
                revert(p, returndatasize())
            }
        }
    }
}

/// @notice Base for tests on the scripted harness: a fresh harness per call and state builders.
abstract contract ScriptedVaultBase is Test {
    bytes32 internal constant T_CHECKPOINT =
        keccak256("Checkpoint(uint64,uint64,uint256,uint64,uint256,uint128,int24,int24)");
    bytes32 internal constant T_BREACH = keccak256("BreachProcessed(uint32,uint40,uint256)");
    bytes32 internal constant T_DEFERRED = keccak256("CheckpointDeferred()");
    bytes32 internal constant T_ACK = keccak256("AckBreaches(uint32)");
    bytes32 internal constant T_MODE = keccak256("ModeChanged(uint8,uint8)");
    bytes32 internal constant T_RECORDED = keccak256("EpochRecorded(uint32,(uint40,uint40,int24,int24,uint64,uint64,uint192,uint8,uint96,uint96,uint128,uint128,uint128,uint128,uint96))");

    function _newHarness() internal returns (VaultHarness h) {
        (Tier memory t,) = Tiers.testing();
        AckRecorder rec = new AckRecorder();
        h = new MockVaultFactory(address(rec), t).deploy(
            type(VaultHarness).creationCode, type(TouchstoneVaultExtension).creationCode
        );
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
