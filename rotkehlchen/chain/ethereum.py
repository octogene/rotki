import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import requests
from web3 import HTTPProvider, Web3
from web3._utils.abi import get_abi_output_types
from web3._utils.contracts import find_matching_event_abi
from web3._utils.filters import construct_event_filter_params

from rotkehlchen.assets.asset import EthereumToken
from rotkehlchen.errors import RemoteError, UnableToDecryptRemoteData
from rotkehlchen.externalapis.etherscan import Etherscan
from rotkehlchen.fval import FVal
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.typing import ChecksumEthAddress
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.misc import from_wei, request_get_dict
from rotkehlchen.utils.serialization import rlk_jsonloads

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

DEFAULT_ETH_RPC_TIMEOUT = 10


def address_to_bytes32(address: ChecksumEthAddress) -> str:
    return '0x' + 24 * '0' + address[2:]


class Ethchain():
    def __init__(
            self,
            ethrpc_endpoint: str,
            etherscan: Etherscan,
            msg_aggregator: MessagesAggregator,
            attempt_connect: bool = True,
            eth_rpc_timeout: int = DEFAULT_ETH_RPC_TIMEOUT,
    ) -> None:
        self.web3: Web3 = None
        self.rpc_endpoint = ethrpc_endpoint
        self.connected = False
        self.etherscan = etherscan
        self.msg_aggregator = msg_aggregator
        self.eth_rpc_timeout = eth_rpc_timeout
        if attempt_connect:
            self.attempt_connect(ethrpc_endpoint)

    def __del__(self) -> None:
        if self.web3:
            del self.web3

    def attempt_connect(
            self,
            ethrpc_endpoint: str,
            mainnet_check: bool = True,
    ) -> Tuple[bool, str]:
        message = ''
        if self.rpc_endpoint == ethrpc_endpoint and self.connected:
            # We are already connected
            return True, 'Already connected to an ethereum node'

        if self.web3:
            del self.web3

        try:
            parsed_eth_rpc_endpoint = urlparse(ethrpc_endpoint)
            if not parsed_eth_rpc_endpoint.scheme:
                ethrpc_endpoint = f"http://{ethrpc_endpoint}"
            provider = HTTPProvider(
                endpoint_uri=ethrpc_endpoint,
                request_kwargs={'timeout': self.eth_rpc_timeout},
            )
            self.web3 = Web3(provider)
        except requests.exceptions.ConnectionError:
            log.warning('Could not connect to an ethereum node. Will use etherscan only')
            self.connected = False
            return False, f'Failed to connect to ethereum node at endpoint {ethrpc_endpoint}'

        if self.web3.isConnected():
            dir_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            with open(os.path.join(dir_path, 'data', 'token_abi.json'), 'r') as f:
                self.token_abi = rlk_jsonloads(f.read())

            # Also make sure we are actually connected to the Ethereum mainnet
            synchronized = True
            msg = ''
            if mainnet_check:
                chain_id = self.web3.eth.chainId
                if chain_id != 1:
                    message = (
                        f'Connected to ethereum node at endpoint {ethrpc_endpoint} but '
                        f'it is not on the ethereum mainnet. The chain id '
                        f'the node is in is {chain_id}.'
                    )
                    log.warning(message)
                    self.connected = False
                    return False, message

                if self.web3.eth.syncing:  # pylint: disable=no-member
                    current_block = self.web3.eth.syncing.currentBlock  # pylint: disable=no-member
                    latest_block = self.web3.eth.syncing.highestBlock  # pylint: disable=no-member
                    synchronized, msg = self.is_synchronized(current_block, latest_block)
                else:
                    current_block = self.web3.eth.blockNumber  # pylint: disable=no-member
                    latest_block = self.query_eth_highest_block()
                    if latest_block is None:
                        msg = 'Could not query latest block'
                        log.warning(msg)
                        synchronized = False
                    else:
                        synchronized, msg = self.is_synchronized(current_block, latest_block)

            if not synchronized:
                self.msg_aggregator.add_warning(
                    'You are using an ethereum node but we could not verify that it is '
                    'synchronized in the ethereum mainnet. Balances and other queries '
                    'may be incorrect.',
                )

            self.connected = True
            log.info(f'Connected to ethereum node at {ethrpc_endpoint}')
            return True, ''
        else:
            log.warning('Could not connect to an ethereum node. Will use etherscan only')
            self.connected = False
            message = f'Failed to connect to ethereum node at endpoint {ethrpc_endpoint}'

        # If we get here we did not connnect
        return False, message

    def is_synchronized(self, current_block: int, latest_block: int) -> Tuple[bool, str]:
        """ Validate that the ethereum node is synchronized
            within 20 blocks of latest block

        Returns a tuple (results, message)
            - result: Boolean for confirmation of synchronized
            - message: A message containing information on what the status is. """
        message = ''
        if current_block < (latest_block - 20):
            message = (
                f'Found ethereum node but it is out of sync. {current_block} / '
                f'{latest_block}. Will use etherscan.'
            )
            log.warning(message)
            self.connected = False
            return False, message

        return True, message

    def set_rpc_endpoint(self, endpoint: str) -> Tuple[bool, str]:
        """ Attempts to set the RPC endpoint for the ethereum client.

        Returns a tuple (result, message)
            - result: Boolean for success or failure of changing the rpc endpoint
            - message: A message containing information on what happened. Can
                       be populated both in case of success or failure"""
        result, message = self.attempt_connect(endpoint)
        if result:
            log.info('Setting ETH RPC endpoint', endpoint=endpoint)
            self.ethrpc_endpoint = endpoint
        return result, message

    def query_eth_highest_block(self) -> Optional[int]:
        """ Attempts to query an external service for the block height

        Returns the highest blockNumber"""

        url = 'https://api.blockcypher.com/v1/eth/main'
        log.debug('Querying blockcypher for ETH highest block', url=url)
        eth_resp: Optional[Dict[str, str]]
        try:
            eth_resp = request_get_dict(url)
        except (RemoteError, UnableToDecryptRemoteData):
            eth_resp = None

        block_number: Optional[int]
        if eth_resp and 'height' in eth_resp:
            block_number = int(eth_resp['height'])
            log.debug('ETH highest block result', block=block_number)
        else:
            try:
                block_number = self.etherscan.get_latest_block_number()
                log.debug('ETH highest block result', block=block_number)
            except RemoteError:
                block_number = None

        return block_number

    def get_eth_balance(self, account: ChecksumEthAddress) -> FVal:
        """Gets the balance of the given account in ETH

        May raise:
        - RemoteError if Etherscan is used and there is a problem querying it or
        parsing its response
        """
        if not self.connected:
            wei_amount = self.etherscan.get_account_balance(account)
        else:
            wei_amount = self.web3.eth.getBalance(account)  # pylint: disable=no-member

        log.debug(
            'Ethereum account balance result',
            sensitive_log=True,
            eth_address=account,
            wei_amount=wei_amount,
        )
        return from_wei(wei_amount)

    def get_multieth_balance(
            self,
            accounts: List[ChecksumEthAddress],
    ) -> Dict[ChecksumEthAddress, FVal]:
        """Returns a dict with keys being accounts and balances in ETH

        May raise:
        - RemoteError if an external service such as Etherscan is queried and
          there is a problem with its query.
        """
        balances: Dict[ChecksumEthAddress, FVal] = {}

        if not self.connected:
            balances = self.etherscan.get_accounts_balance(accounts)
        else:
            for account in accounts:
                amount = FVal(self.web3.eth.getBalance(account))  # pylint: disable=no-member
                log.debug(
                    'Ethereum node balance result',
                    sensitive_log=True,
                    eth_address=account,
                    wei_amount=amount,
                )
                balances[account] = from_wei(amount)

        return balances

    def get_multitoken_balance(
            self,
            token: EthereumToken,
            accounts: List[ChecksumEthAddress],
    ) -> Dict[ChecksumEthAddress, FVal]:
        """Return a dictionary with keys being accounts and value balances of token
        Balance value is normalized through the token decimals.

        May raise:
        - RemoteError if an external service such as Etherscan is queried and
          there is a problem with its query.
        - BadFunctionCallOutput if a local node is used and the contract for the
          token has no code. That means the chain is not synced
        """
        balances = {}
        if self.connected:
            token_contract = self.web3.eth.contract(  # pylint: disable=no-member
                address=token.ethereum_address,
                abi=self.token_abi,
            )

            for account in accounts:
                log.debug(
                    'Ethereum node query for token balance',
                    sensitive_log=True,
                    eth_address=account,
                    token_address=token.ethereum_address,
                    token_symbol=token.decimals,
                )
                token_amount = FVal(token_contract.functions.balanceOf(account).call())
                if token_amount != 0:
                    balances[account] = token_amount / (FVal(10) ** FVal(token.decimals))
                log.debug(
                    'Ethereum node result for token balance',
                    sensitive_log=True,
                    eth_address=account,
                    token_address=token.ethereum_address,
                    token_symbol=token.symbol,
                    amount=token_amount,
                )
        else:
            for account in accounts:
                balances[account] = self.etherscan.get_token_balance(token, account)
                log.debug(
                    'Etherscan result for token balance',
                    sensitive_log=True,
                    eth_address=account,
                    token_address=token.ethereum_address,
                    token_symbol=token.symbol,
                    amount=balances[account],
                )

        return balances

    def get_token_balance(
            self,
            token: EthereumToken,
            account: ChecksumEthAddress,
    ) -> FVal:
        """Returns the balance of account in token.
        Balance value is normalized through the token decimals.

        May raise:
        - RemoteError if an external service such as Etherscan is queried and
        there is a problem with its query.
        - BadFunctionCallOutput if a local node is used and the contract for the
        token has no code. That means the chain is not synced
        """
        res = self.get_multitoken_balance(token=token, accounts=[account])
        return res.get(account, FVal(0))

    def get_block_by_number(self, num: int) -> Optional[Dict[str, Any]]:
        if not self.connected:
            return None

        return self.web3.eth.getBlock(num)  # pylint: disable=no-member

    def get_code(self, account: ChecksumEthAddress) -> str:
        """Gets the deployment bytecode at the given address

        May raise:
        - RemoteError if Etherscan is used and there is a problem querying it or
        parsing its response
        """
        if self.connected:
            result = self.web3.eth.getCode(account)
        else:
            result = self.etherscan.get_code(account)

        return result

    def _check_contract_etherscan(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            method_name: str,
            arguments: Optional[List[Any]] = None,
    ):
        web3 = Web3()
        contract = web3.eth.contract(address=contract_address, abi=abi)
        input_data = contract.encodeABI(method_name, args=arguments if arguments else [])
        result = self.etherscan.eth_call(
            to_address=contract_address,
            input_data=input_data,
        )
        fn_abi = contract._find_matching_fn_abi(
            fn_identifier=method_name,
            args=arguments,
        )
        output_types = get_abi_output_types(fn_abi)
        output_data = web3.codec.decode_abi(output_types, bytes.fromhex(result[2:]))

        if len(output_data) != 1:
            log.error('Unexpected call with multiple output data. Can not handle properly')
        return output_data[0]

    def check_contract(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            method_name: str,
            arguments: Optional[List[Any]] = None,
    ):
        if self.connected:
            contract = self.web3.eth.contract(address=contract_address, abi=abi)
            method = getattr(contract.caller, method_name)
            return method(*arguments if arguments else [])
        else:
            return self._check_contract_etherscan(
                contract_address=contract_address,
                abi=abi,
                method_name=method_name,
                arguments=arguments,
            )

    def get_logs(
            self,
            contract_address: ChecksumEthAddress,
            abi: List,
            event_name: str,
            argument_filters: Dict[str, str],
            from_block: Union[int, str],
            to_block: Union[int, str] = 'latest',
    ) -> List[Dict[str, Any]]:
        if self.connected:
            event_abi = find_matching_event_abi(abi=abi, event_name=event_name)
            _, filter_args = construct_event_filter_params(
                event_abi=event_abi,
                abi_codec=self.web3.codec,
                contract_address=contract_address,
                argument_filters=argument_filters,
                fromBlock=from_block,
                toBlock=to_block,
            )
            if event_abi['anonymous']:
                # web3.py does not handle the anonymous events correctly and adds the first topic
                filter_args['topics'] = filter_args['topics'][1:]

            until_block = self.web3.eth.blockNumber if to_block == 'latest' else to_block
            events = []
            start_block = from_block
            while start_block <= until_block:
                filter_args['fromBlock'] = start_block
                end_block = min(start_block + 250000, until_block)
                filter_args['toBlock'] = end_block
                log.debug(
                    'Querying contract event',
                    contract_address=contract_address,
                    event_name=event_name,
                    argument_filters=argument_filters,
                    from_block=filter_args['fromBlock'],
                    to_block=filter_args['toBlock'],
                )
                # WTF: for some reason the first time we get in here the loop resets
                # to the start without querying eth_getLogs and ends up with double logging
                new_events = self.web3.eth.getLogs(filter_args)
                start_block = end_block + 1
                events.extend(new_events)
        else:
            pass

        return events