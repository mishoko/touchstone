// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Entry} from "../types/Entry.sol";

/// @notice The vault's mode machine: transitions on breach, placement and retirement, and the entry-point table.
/// @dev Modes: 0 NORMAL, 1 BREACHED, 2 EXITED. Events: 0 Breach, 1 Place, 2 Retire (their own domain).
library Ladder {
    uint8 internal constant NORMAL = 0;
    uint8 internal constant BREACHED = 1;
    uint8 internal constant EXITED = 2;
    uint8 internal constant EV_BREACH = 0;
    uint8 internal constant EV_PLACE = 1;
    uint8 internal constant EV_RETIRE = 2;
    /// @notice The breach count that removes the curator.
    uint32 internal constant REMOVAL = 3;

    error BadMode();

    /// @dev Entries open in NORMAL and BREACHED: everything except `ReleaseBond` and `Seed`.
    uint256 internal constant OPEN_LIVE = (1 << uint256(Entry.Deposit)) | (1 << uint256(Entry.Withdraw))
        | (1 << uint256(Entry.SetPositions)) | (1 << uint256(Entry.RebalanceSwap)) | (1 << uint256(Entry.Claim))
        | (1 << uint256(Entry.TopUpBond)) | (1 << uint256(Entry.Retire)) | (1 << uint256(Entry.Poke))
        | (1 << uint256(Entry.ProcessBreach)) | (1 << uint256(Entry.Settle));
    /// @dev Entries open in EXITED.
    // [MU-045] deposit and topUpBond stay closed in EXITED; [MU-064] claim stays open
    uint256 internal constant OPEN_EXITED = (1 << uint256(Entry.Withdraw)) | (1 << uint256(Entry.Claim))
        | (1 << uint256(Entry.ReleaseBond)) | (1 << uint256(Entry.Poke)) | (1 << uint256(Entry.ProcessBreach))
        | (1 << uint256(Entry.Settle));

    /// @return mode the mode after the event
    /// @return breaches the breach count after the event (saturating)
    /// @return exited this call moved the mode from NORMAL or BREACHED to EXITED
    function next(uint8 mode, uint32 breaches, uint8 ev, uint32 count) internal pure returns (uint8, uint32, bool) {
        if (ev == EV_BREACH) {
            if (count == 0) return (mode, breaches, false);
            uint256 b = uint256(breaches) + count;
            uint32 nb = b > type(uint32).max ? type(uint32).max : uint32(b);
            if (mode == EXITED) return (EXITED, nb, false);
            // [MU-048] [MU-053] removal at the third breach, counted cumulatively
            if (nb >= REMOVAL) return (EXITED, nb, true);
            return (BREACHED, nb, false);
        }
        if (mode == EXITED) revert BadMode();
        if (ev == EV_PLACE) return (NORMAL, breaches, false);
        if (ev == EV_RETIRE) return (EXITED, breaches, true);
        revert BadMode();
    }

    function allowed(uint8 mode, uint8 entry) internal pure returns (bool) {
        uint256 open = mode == EXITED ? OPEN_EXITED : OPEN_LIVE;
        return (open >> entry) & 1 == 1;
    }
}
