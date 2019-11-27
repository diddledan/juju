// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package instancepoller

import (
	"fmt"

	"github.com/juju/clock"
	"github.com/juju/errors"
	"gopkg.in/juju/names.v3"

	"github.com/juju/juju/apiserver/common"
	"github.com/juju/juju/apiserver/facade"
	"github.com/juju/juju/apiserver/params"
	"github.com/juju/juju/core/network"
	"github.com/juju/juju/core/status"
	"github.com/juju/juju/state"
)

// InstancePollerAPI provides access to the InstancePoller API facade.
type InstancePollerAPI struct {
	*common.LifeGetter
	*common.ModelWatcher
	*common.ModelMachinesWatcher
	*common.InstanceIdGetter
	*common.StatusGetter

	st            StateInterface
	resources     facade.Resources
	authorizer    facade.Authorizer
	accessMachine common.GetAuthFunc
	clock         clock.Clock
}

// NewFacade wraps NewInstancePollerAPI for facade registration.
func NewFacade(
	st *state.State,
	resources facade.Resources,
	authorizer facade.Authorizer,
) (*InstancePollerAPI, error) {
	m, err := st.Model()
	if err != nil {
		return nil, errors.Trace(err)
	}
	return NewInstancePollerAPI(st, m, resources, authorizer, clock.WallClock)
}

// NewInstancePollerAPI creates a new server-side InstancePoller API
// facade.
func NewInstancePollerAPI(
	st *state.State,
	m *state.Model,
	resources facade.Resources,
	authorizer facade.Authorizer,
	clock clock.Clock,
) (*InstancePollerAPI, error) {

	if !authorizer.AuthController() {
		// InstancePoller must run as a controller.
		return nil, common.ErrPerm
	}
	accessMachine := common.AuthFuncForTagKind(names.MachineTagKind)
	sti := getState(st, m)

	// Life() is supported for machines.
	lifeGetter := common.NewLifeGetter(
		sti,
		accessMachine,
	)
	// ModelConfig() and WatchForModelConfigChanges() are allowed
	// with unrestricted access.
	modelWatcher := common.NewModelWatcher(
		sti,
		resources,
		authorizer,
	)
	// WatchModelMachines() is allowed with unrestricted access.
	machinesWatcher := common.NewModelMachinesWatcher(
		sti,
		resources,
		authorizer,
	)
	// InstanceId() is supported for machines.
	instanceIdGetter := common.NewInstanceIdGetter(
		sti,
		accessMachine,
	)
	// Status() is supported for machines.
	statusGetter := common.NewStatusGetter(
		sti,
		accessMachine,
	)

	return &InstancePollerAPI{
		LifeGetter:           lifeGetter,
		ModelWatcher:         modelWatcher,
		ModelMachinesWatcher: machinesWatcher,
		InstanceIdGetter:     instanceIdGetter,
		StatusGetter:         statusGetter,
		st:                   sti,
		resources:            resources,
		authorizer:           authorizer,
		accessMachine:        accessMachine,
		clock:                clock,
	}, nil
}

func (a *InstancePollerAPI) getOneMachine(tag string, canAccess common.AuthFunc) (StateMachine, error) {
	machineTag, err := names.ParseMachineTag(tag)
	if err != nil {
		return nil, err
	}
	if !canAccess(machineTag) {
		return nil, common.ErrPerm
	}
	entity, err := a.st.FindEntity(machineTag)
	if err != nil {
		return nil, err
	}
	machine, ok := entity.(StateMachine)
	if !ok {
		return nil, common.NotSupportedError(
			machineTag, fmt.Sprintf("expected machine, got %T", entity),
		)
	}
	return machine, nil
}

// SetProviderNetworkConfig updates the provider addresses for one or more
// machines.
func (a *InstancePollerAPI) SetProviderNetworkConfig(req params.SetProviderNetworkConfig) (params.SetProviderNetworkConfigResults, error) {
	result := params.SetProviderNetworkConfigResults{
		Results: make([]params.SetProviderNetworkConfigResult, len(req.Args)),
	}

	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}

	spaceInfos, err := a.st.AllSpaceInfos()
	if err != nil {
		return result, err
	}

	for i, arg := range req.Args {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		newProviderAddrs, err := mapNetworkConfigsToProviderAddresses(arg.Configs, spaceInfos)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		// spaceIDs have already been resolved for us by the above
		// function call; we can safely provide a nil lookup to the
		// converter and ignore errors.
		newSpaceAddrs, _ := newProviderAddrs.ToSpaceAddresses(nil)

		modified, err := maybeUpdateMachineProviderAddresses(machine, newSpaceAddrs)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		result.Results[i].Modified = modified
		result.Results[i].Addresses = params.FromProviderAddresses(newProviderAddrs...)
	}
	return result, nil
}

func maybeUpdateMachineProviderAddresses(m StateMachine, newSpaceAddrs network.SpaceAddresses) (bool, error) {
	curSpaceAddrs := m.ProviderAddresses()
	if curSpaceAddrs.EqualTo(newSpaceAddrs) {
		return false, nil
	}

	return true, m.SetProviderAddresses(newSpaceAddrs...)
}

// mapNetworkConfigsToProviderAddresses iterates the list of incoming network
// configuration parameters, extracts all usable private/shadow IP addresses,
// attempts to resolve each one to a known space and returns back a list of
// scoped, space-aware ProviderAddresses.
func mapNetworkConfigsToProviderAddresses(cfgs []params.NetworkConfig, spaceInfos network.SpaceInfos) (network.ProviderAddresses, error) {
	addrs := make(network.ProviderAddresses, 0, len(cfgs))

	alphaSpaceInfo := spaceInfos.GetByID(network.AlphaSpaceId)
	if alphaSpaceInfo == nil {
		return network.ProviderAddresses{}, errors.New("BUG: space infos lack entry for alpha space")
	}

	for _, cfg := range cfgs {
		// Private addresses use the same CIDR; try to resolve them
		// to a space and create a scoped network address
		for _, addr := range params.ToProviderAddresses(cfg.Addresses...) {
			spaceInfo, err := spaceInfoForAddress(spaceInfos, cfg.CIDR, cfg.ProviderSubnetId, addr.Value)
			if err != nil {
				// If we were unable to infer the space using the
				// currently available subnet information, use
				// alpha space as a fall-back
				if !errors.IsNotFound(err) {
					return network.ProviderAddresses{}, err
				}
				spaceInfo = alphaSpaceInfo
			}

			addrs = append(
				addrs,
				network.NewScopedProviderAddressFromSpaceInfo(addr.Value, addr.Scope, spaceInfo),
			)
		}

		for _, addr := range params.ToProviderAddresses(cfg.ShadowAddresses...) {
			// Try to infer space from the address value only; The CIDR
			// information from cfg does not apply to these addresses.
			spaceInfo, err := spaceInfoForAddress(spaceInfos, "", "", addr.Value)
			if err != nil {
				// Space inference will always fail for public shadow addresses.
				// For those cases we auto-assign the alpha space. In the
				// future we might want to consider defining a public-alpha
				// space.
				if !errors.IsNotFound(err) {
					return network.ProviderAddresses{}, err
				}
				spaceInfo = alphaSpaceInfo
			}
			addrs = append(
				addrs,
				network.NewScopedProviderAddressFromSpaceInfo(addr.Value, addr.Scope, spaceInfo),
			)
		}
	}

	return addrs, nil
}

func spaceInfoForAddress(spaceInfos network.SpaceInfos, CIDR, providerSubnetID, addr string) (*network.SpaceInfo, error) {
	if CIDR != "" && providerSubnetID != "" {
		return spaceInfos.InferSpaceFromCIDRAndSubnetID(CIDR, providerSubnetID)
	} else if CIDR != "" {
		return spaceInfos.InferSpaceFromCIDR(CIDR)
	}
	return spaceInfos.InferSpaceFromAddress(addr)
}

// ProviderAddresses returns the list of all known provider addresses
// for each given entity. Only machine tags are accepted.
func (a *InstancePollerAPI) ProviderAddresses(args params.Entities) (params.MachineAddressesResults, error) {
	result := params.MachineAddressesResults{
		Results: make([]params.MachineAddressesResult, len(args.Entities)),
	}
	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}
	for i, arg := range args.Entities {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		addrs, err := machine.ProviderAddresses().ToProviderAddresses(a.st)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		result.Results[i].Addresses = params.FromProviderAddresses(addrs...)

	}
	return result, nil
}

// SetProviderAddresses updates the list of known provider addresses
// for each given entity. Only machine tags are accepted.
func (a *InstancePollerAPI) SetProviderAddresses(args params.SetMachinesAddresses) (params.ErrorResults, error) {
	result := params.ErrorResults{
		Results: make([]params.ErrorResult, len(args.MachineAddresses)),
	}
	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}
	for i, arg := range args.MachineAddresses {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}

		addrsToSet, err := params.ToProviderAddresses(arg.Addresses...).ToSpaceAddresses(a.st)
		if err != nil {
			result.Results[i].Error = common.ServerError(err)
			continue
		}
		if err := machine.SetProviderAddresses(addrsToSet...); err != nil {
			result.Results[i].Error = common.ServerError(err)
		}
	}
	return result, nil
}

// InstanceStatus returns the instance status for each given entity.
// Only machine tags are accepted.
func (a *InstancePollerAPI) InstanceStatus(args params.Entities) (params.StatusResults, error) {
	result := params.StatusResults{
		Results: make([]params.StatusResult, len(args.Entities)),
	}
	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}
	for i, arg := range args.Entities {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err == nil {
			var statusInfo status.StatusInfo
			statusInfo, err = machine.InstanceStatus()
			result.Results[i].Status = statusInfo.Status.String()
			result.Results[i].Info = statusInfo.Message
			result.Results[i].Data = statusInfo.Data
			result.Results[i].Since = statusInfo.Since
		}
		result.Results[i].Error = common.ServerError(err)
	}
	return result, nil
}

// SetInstanceStatus updates the instance status for each given
// entity. Only machine tags are accepted.
func (a *InstancePollerAPI) SetInstanceStatus(args params.SetStatus) (params.ErrorResults, error) {
	result := params.ErrorResults{
		Results: make([]params.ErrorResult, len(args.Entities)),
	}
	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}
	for i, arg := range args.Entities {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err == nil {
			now := a.clock.Now()
			s := status.StatusInfo{
				Status:  status.Status(arg.Status),
				Message: arg.Info,
				Data:    arg.Data,
				Since:   &now,
			}
			err = machine.SetInstanceStatus(s)
			if status.Status(arg.Status) == status.ProvisioningError {
				s.Status = status.Error
				if err == nil {
					err = machine.SetStatus(s)
				}
			}
		}
		result.Results[i].Error = common.ServerError(err)
	}
	return result, nil
}

// AreManuallyProvisioned returns whether each given entity is
// manually provisioned or not. Only machine tags are accepted.
func (a *InstancePollerAPI) AreManuallyProvisioned(args params.Entities) (params.BoolResults, error) {
	result := params.BoolResults{
		Results: make([]params.BoolResult, len(args.Entities)),
	}
	canAccess, err := a.accessMachine()
	if err != nil {
		return result, err
	}
	for i, arg := range args.Entities {
		machine, err := a.getOneMachine(arg.Tag, canAccess)
		if err == nil {
			result.Results[i].Result, err = machine.IsManual()
		}
		result.Results[i].Error = common.ServerError(err)
	}
	return result, nil
}
