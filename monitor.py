from math import sqrt
from okex_api import *
import funding_rate
import open_position
import close_position
import trading_data
from utils import *
import multiprocessing


# 监控一个币种，如果当期资金费+预测资金费小于重新开仓成本（开仓期现差价-平仓期现差价-手续费），进行平仓。
# 如果合约仓位到达下级杠杆，进行减仓。如果合约仓位到达上级杠杆，进行加仓。
# 如果距离下期资金费3小时以上，（开仓期现差价-平仓期现差价-手续费）>0.2%，进行套利。
class Monitor(OKExAPI):
    """监控功能类
    """

    @property
    def __name__(self):
        return 'Monitor'

    def __init__(self, coin=None, accountid=3):
        super().__init__(coin=coin, accountid=accountid)

    async def apr(self, days=0):
        """最近年利率

        :param days: 最近几天，默认开仓算起
        :rtype: float
        """
        Stat, holding = await gather(trading_data.Stat(self.coin), self.swap_holding())
        margin = holding['margin']
        upl = holding['upl']
        last = holding['last']
        position = - holding['pos'] * float(self.swap_info['ctVal'])
        size = position * last + margin + upl

        if size > 10:
            if days == 0:
                open_time = Stat.open_time(self.accountid)
                delta = (datetime.utcnow() - open_time).total_seconds()
                funding = Stat.history_funding(self.accountid)
                cost = Stat.history_cost(self.accountid)
                apr = (funding + cost) / size / delta * 86400 * 365
            else:
                funding = Stat.history_funding(self.accountid, days)
                cost = Stat.history_cost(self.accountid, days)
                apr = (funding + cost) / size / days * 365
        else:
            apr = 0.
        return apr

    async def back_tracking(self):
        """补录最近三个月资金费
        """
        Ledger = record.Record('Ledger')
        pipeline = [{'$match': {'account': self.accountid, 'instrument': self.coin, 'title': '资金费'}}]
        # Results in DB
        db_ledger = [n for n in Ledger.mycol.aggregate(pipeline)]
        inserted = 0
        api_ledger = await get_with_limit(self.accountAPI.get_archive_ledger, tag='billId', max=100, limit=0,
                                          sem=self.asem, sleep=self.asleep, instType='SWAP', ccy='USDT', type='8')
        # API results
        for item in api_ledger:
            if item['instId'] == self.swap_ID:
                realized_rate = float(item['pnl'])
                timestamp = datetime.utcfromtimestamp(float(item['ts']) / 1000)
                mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, title='资金费',
                              funding=realized_rate)
                # 查重
                for n in db_ledger:
                    if n['funding'] == realized_rate:
                        if n['timestamp'] == timestamp:
                            break
                else:
                    Ledger.mycol.insert_one(mydict)
                    inserted += 1
        fprint(lang.back_track_funding.format(self.coin, inserted))

    async def record_funding(self):
        """记录最近一次资金费
        """
        # /api/v5/account/bills 限速：5次/s
        sem = self.psem if self.psem else multiprocessing.Semaphore(1)
        with sem:
            Ledger = record.Record('Ledger')
            ledger = await self.accountAPI.get_ledger(instType='SWAP', ccy='USDT', type='8')
            if self.psem: await asyncio.sleep(1)
        realized_rate = 0.
        for item in ledger:
            if item['instId'] == self.swap_ID:
                realized_rate = float(item['pnl'])
                timestamp = datetime.utcfromtimestamp(float(item['ts']) / 1000)
                mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, title='资金费',
                              funding=realized_rate)
                Ledger.mycol.insert_one(mydict)
                break
        fprint(lang.received_funding.format(self.coin, realized_rate))

    @call_coroutine
    async def position_exist(self, swap_ID=None):
        """判断是否有仓位
        """
        if not swap_ID: swap_ID = self.swap_ID
        if await self.swap_position(swap_ID) == 0:
            # fprint(lang.nonexistent_position.format(swap_ID))
            return False
        else:
            result = record.Record('Ledger').find_last(dict(account=self.accountid, instrument=self.coin))
            if result and result['title'] == '平仓':
                fprint(lang.has_closed.format(self.swap_ID))
                return False
        return True

    @run_with_cancel
    async def watch(self):
        """监控仓位，自动加仓、减仓
        """
        if not await self.position_exist():
            fprint(lang.nonexistent_position.format(self.swap_ID))
            return

        fundingRate = funding_rate.FundingRate()
        addPosition: open_position.AddPosition
        reducePosition: close_position.ReducePosition
        Stat: trading_data.Stat
        addPosition, reducePosition, Stat = await gather(open_position.AddPosition(self.coin, self.accountid),
                                                         close_position.ReducePosition(self.coin, self.accountid),
                                                         trading_data.Stat(self.coin))
        Ledger = record.Record('Ledger')
        # OP = record.Record('OP')

        portfolio = record.Record('Portfolio').mycol.find_one(dict(account=self.accountid, instrument=self.coin))
        leverage = portfolio['leverage']
        if 'size' not in portfolio:
            portfolio = await self.update_portfolio()
        size = portfolio['size']
        fprint(lang.start_monitoring.format(self.coin, size, leverage))

        # 计算手续费
        spot_trade_fee, swap_trade_fee, liquidation_price = await gather(
            self.accountAPI.get_trade_fee(instType='SPOT', instId=self.spot_ID),
            self.accountAPI.get_trade_fee(instType='SWAP', uly=self.spot_ID),
            self.liquidation_price())
        spot_trade_fee = float(spot_trade_fee['taker'])
        swap_trade_fee = float(swap_trade_fee['taker'])
        trade_fee = swap_trade_fee + spot_trade_fee

        updated = False
        task_started = False
        time_to_accelerate = None
        accelerated = False
        adding = False
        reducing = False
        margin_reducible = True
        self.exitFlag = False

        while not self.exitFlag:
            begin = timestamp = datetime.utcnow()
            swap_ticker = await self.publicAPI.get_specific_ticker(self.swap_ID)
            last = float(swap_ticker['last'])

            # 每小时更新一次资金费，强平价
            if not updated and timestamp.minute == 1:
                (current_rate, next_rate), liquidation_price = await gather(fundingRate.current_next(self.swap_ID),
                                                                            self.liquidation_price())
                assert liquidation_price

                assert (recent := Stat.recent_open_stat()), lang.fetch_ticker_first
                open_pd = recent['avg'] + recent['std']
                recent = Stat.recent_close_stat()
                close_pd = recent['avg'] - recent['std']
                cost = open_pd - close_pd + 2 * trade_fee
                if (timestamp.hour + 4) % 8 == 0 and current_rate + next_rate < cost:
                    fprint(lang.coin_current_next)
                    fprint(f'{self.coin:6s}{current_rate:9.3%}{next_rate:11.3%}')
                    fprint(lang.cost_to_close.format(cost))
                    fprint(lang.closing.format(self.coin))
                    await reducePosition.close(price_diff=close_pd)
                    break

                if timestamp.hour % 8 == 0:
                    await self.record_funding()
                    fprint(lang.coin_current_next)
                    fprint(f'{self.coin:6s}{current_rate:9.3%}{next_rate:11.3%}')
                updated = True
            elif updated and timestamp.minute == 2:
                updated = False

            # 线程未创建
            if not task_started:
                # 接近强平价，现货减仓
                if liquidation_price < last * (1 + 1 / (leverage + 1)):
                    # 等待上一操作完成
                    # if OP.find_last({'account': self.accountid, 'instrument': self.coin}):
                    #     timestamp = datetime.utcnow()
                    #     delta = (timestamp - begin).total_seconds()
                    #     if delta < 10:
                    #         await asyncio.sleep(10 - delta)
                    #     continue
                    if not await addPosition.is_hedged():
                        spot, swap = await gather(self.spot_position(), self.swap_position())
                        fprint(lang.hedge_fail.format(self.coin, spot, swap))
                        exit()
                    fprint(lang.approaching_liquidation)
                    mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, title='自动减仓')
                    Ledger.mycol.insert_one(mydict)

                    # 期现差价控制在2个标准差
                    assert (recent := Stat.recent_close_stat()), lang.fetch_ticker_first
                    close_pd = recent['avg'] - 2 * recent['std']

                    swap_position = await self.swap_position()
                    target_size = swap_position / (leverage + 1) ** 2

                    reduce_task = create_task(reducePosition.reduce(target_size=target_size, price_diff=close_pd))
                    task_started = True
                    reducing = True
                    time_to_accelerate = datetime.utcnow() + timedelta(hours=2)

                # 保证金过多，现货加仓
                if margin_reducible and liquidation_price > last * (1 + 1 / (leverage - 1)):
                    # 等待上一操作完成
                    # if OP.find_last({'account': self.accountid, 'instrument': self.coin}):
                    #     timestamp = datetime.utcnow()
                    #     delta = (timestamp - begin).total_seconds()
                    #     if delta < 10:
                    #         await asyncio.sleep(10 - delta)
                    #     continue
                    if not await addPosition.is_hedged():
                        spot, swap = await gather(self.spot_position(), self.swap_position())
                        fprint(lang.hedge_fail.format(self.coin, spot, swap))
                        exit()
                    fprint(lang.too_much_margin)
                    mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, title='自动加仓')
                    Ledger.mycol.insert_one(mydict)

                    # 期现差价控制在2个标准差
                    assert (recent := Stat.recent_open_stat()), lang.fetch_ticker_first
                    open_pd = recent['avg'] + 2 * recent['std']

                    # swap_position = await self.swap_position()
                    # target_size = swap_position * (liquidation_price / last / (1 + 1 / leverage) - 1)
                    usdt_size = await addPosition.adjust_swap_lever(leverage)
                    if usdt_size:
                        add_task = create_task(addPosition.add(usdt_size=usdt_size, price_diff=open_pd))
                        task_started = True
                        adding = True
                        time_to_accelerate = datetime.utcnow() + timedelta(hours=2)
                    else:
                        fprint(lang.no_margin_reduce)
                        margin_reducible = False
            # 线程已运行
            else:
                # 如果减仓时间过长，加速减仓
                if reducing and not reduce_task.done():
                    # 迫近下下级杠杆
                    if liquidation_price < last * (1 + 1 / (leverage + 2)) and not accelerated:
                        # 已加速就不另开线程
                        reducePosition.exitFlag = True
                        while not reduce_task.done():
                            await asyncio.sleep(1)

                        assert (recent := Stat.recent_close_stat(1)), lang.fetch_ticker_first
                        close_pd = recent['avg'] - 1.5 * recent['std']

                        liquidation_price, swap_position = await gather(self.liquidation_price(), self.swap_position())
                        target_size = swap_position * (1 - liquidation_price / last / (1 + 1 / leverage))
                        reduce_task = create_task(reducePosition.reduce(target_size=target_size, price_diff=close_pd))
                        reducing = True
                        accelerated = True
                        time_to_accelerate = datetime.utcnow() + timedelta(hours=2)

                    if timestamp > time_to_accelerate:
                        reducePosition.exitFlag = True
                        while not reduce_task.done():
                            await asyncio.sleep(1)

                        assert (recent := Stat.recent_close_stat(2)), lang.fetch_ticker_first
                        close_pd = recent['avg'] - 2 * recent['std']

                        liquidation_price, swap_position = await gather(self.liquidation_price(), self.swap_position())
                        target_size = swap_position * (1 - liquidation_price / last / (1 + 1 / leverage))
                        reduce_task = create_task(reducePosition.reduce(target_size=target_size, price_diff=close_pd))
                        reducing = True
                        time_to_accelerate = datetime.utcnow() + timedelta(hours=2)
                elif adding and not add_task.done():
                    if timestamp > time_to_accelerate:
                        addPosition.exitFlag = True
                        while not add_task.done():
                            await asyncio.sleep(1)

                        assert (recent := Stat.recent_open_stat(2)), lang.fetch_ticker_first
                        open_pd = recent['avg'] + 2 * recent['std']

                        # liquidation_price = await self.liquidation_price()
                        # swap_position = await self.swap_position()
                        # target_size = swap_position * (liquidation_price / last / (1 + 1 / leverage) - 1)
                        if (usdt_size := usdt_size - add_task.result()) > 0:
                            add_task = create_task(addPosition.add(usdt_size=usdt_size, price_diff=open_pd))
                            adding = True
                            time_to_accelerate = datetime.utcnow() + timedelta(hours=2)
                else:
                    liquidation_price = await self.liquidation_price()
                    adding = reducing = task_started = False
            delta = (datetime.utcnow() - begin).total_seconds()
            if delta < 10:
                await asyncio.sleep(10 - delta)

    @run_with_cancel
    async def amm(self, usdt=0, grid_size=0.01):
        begin = timestamp = datetime.utcnow()
        # Risk-free interest rate
        r = 0.05
        grid_num = 20
        min_size = float(self.spot_info['minSz'])
        size_increment = float(self.spot_info['lotSz'])
        size_decimals = num_decimals(self.spot_info['lotSz'])
        tick_size = float(self.spot_info['tickSz'])
        tick_decimals = num_decimals(self.spot_info['tickSz'])
        trade_fee, usdt_balance, spot_ticker, spot_position = await gather(
            self.accountAPI.get_trade_fee(instType='SPOT', instId=self.spot_ID), self.usdt_balance(),
            self.publicAPI.get_specific_ticker(self.spot_ID), self.spot_position())
        taker_fee = float(trade_fee['taker'])
        maker_fee = float(trade_fee['maker'])
        initial_price = last = float(spot_ticker['last'])

        Record = record.Record('AMM')
        # Manage existing LP position
        if usdt == 0:
            mydict = Record.find_last(dict(account=self.accountid, instrument=self.coin, op='open'))
            k = mydict['k']
            begin = mydict['timestamp']
            initial_price = mydict['price']
        # Open new LP position
        else:
            assert usdt_balance > usdt / 2, print(lang.insufficient_USDT)
            # n1*n2=k n1=sqrt(k/p1) v=2*n1*p1=2*sqrt(k*p1)
            k = (usdt / 2) ** 2 / initial_price
            mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, k=k, op='open',
                          price=initial_price)
            Record.mycol.insert_one(mydict)

        n1 = ni = sqrt(k / last)
        if spot_position < ni:
            spot_size = round_to((ni - spot_position) / (1 + taker_fee), size_increment)
            if spot_size >= min_size:
                spot_order = await self.tradeAPI.take_spot_order(instId=self.spot_ID, side='buy', size=spot_size,
                                                                 tgtCcy='base_ccy', order_type='market')
                assert spot_order['ordId'] != '-1', print(spot_order)
                if usdt == 0:
                    spot_order = await self.tradeAPI.get_order_info(instId=self.spot_ID, order_id=spot_order['ordId'])
                    spot_filled = float(spot_order['accFillSz']) + float(spot_order['fee'])
                    spot_price = float(spot_order['avgPx'])
                    spot_fee = float(spot_order['fee']) * spot_price
                    spot_notional = - k * spot_filled / spot_position / (spot_position + spot_filled)
                    mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp, op='buy', k=k,
                                  cash_notional=- spot_filled * spot_price, spot_notional=spot_notional, fee=spot_fee)
                    Record.mycol.insert_one(mydict)

        def stat():
            pipeline = [
                {'$match': {'account': self.accountid, 'instrument': self.coin, 'timestamp': {'$gt': begin}}},
                {'$group': {'_id': '$instrument', 'cash_notional': {'$sum': '$cash_notional'},
                            'spot_notional': {'$sum': '$spot_notional'}, 'fee': {'$sum': '$fee'}}}]
            res = [x for x in Record.mycol.aggregate(pipeline)]
            if res:
                cash_notional = res[0]['cash_notional']
                spot_notional = res[0]['spot_notional']
                fee_total = res[0]['fee']
                spot_price = k / n1 ** 2
                lp_value = 2. * k / n1
                lp_pnl = cash_notional - spot_notional + fee_total
                period = (datetime.utcnow() - begin).total_seconds() / 86400 / 365
                theta = lp_pnl / period
                gamma = - 0.5 * n1 / spot_price
                # theta + 0.5 * sigma**2 * spot_price**2 * gamma + r * (spot_price * delta - lp_value) == 0
                sigma2 = - (theta - r * 0.5 * lp_value) / (0.5 * spot_price ** 2 * gamma)
                if sigma2 > 0:
                    sigma = sqrt(sigma2)
                    print(f'LP APR={theta / lp_value:7.2%}')
                    print(f'Realized volatility={sigma:7.2%}')
                else:
                    print(f'{sigma2=:7.2%}, {(theta - r * 0.5 * lp_value) / lp_value=:7.2%}')
                    print(f'{2. * sqrt(k * initial_price):8.2f}, {cash_notional + fee_total:8.2f},'
                          f' {spot_notional:8.2f}, {lp_value:8.2f}')

        stat()

        async def cancel_orders():
            pending = await self.tradeAPI.pending_order(instType='SPOT', instId=self.spot_ID, state='live')
            orders = [dict(instId=self.spot_ID, ordId=order['ordId']) if order['ordId']
                      else dict(instId=self.spot_ID, clOrdId=order['clOrdId']) for order in pending]
            if orders:
                await self.tradeAPI.batch_cancel(orders)
                print(f'Cancelled {len(orders)} orders.')

        await cancel_orders()

        grids = []
        a = sqrt(1 + grid_size)
        b = 1 / a

        async def init_grids():
            nonlocal grids
            orders = []
            grids.append(dict(index=grid_num // 2, price=last, order=None))
            index = grid_num // 2 + 1
            sell_price = round_to(last * a ** 2, tick_size)
            sell_size = round_to(ni * (1 - b), size_increment)
            while sell_size >= min_size and abs(index - grid_num // 2) <= grid_num // 2:
                order = dict(instId=self.spot_ID, tdMode='cash', side='sell', ordType='limit',
                             clOrdId=self.coin + 'grid' + str(index), px=float_str(sell_price, tick_decimals),
                             sz=float_str(sell_size, size_decimals))
                orders.append(order)
                grid = dict(index=index, price=sell_price, order=order)
                grids.append(grid)
                index += 1
                sell_price = round_to(sell_price * a ** 2, tick_size)
                sell_size = round_to(sell_size * b, size_increment)

            index = grid_num // 2 - 1
            buy_price = round_to(last * b ** 2, tick_size)
            buy_size = round_to(ni * (a - 1) / (1 + maker_fee), size_increment)
            while buy_size >= min_size and abs(index - grid_num // 2) <= grid_num // 2:
                order = dict(instId=self.spot_ID, tdMode='cash', side='buy', ordType='limit',
                             clOrdId=self.coin + 'grid' + str(index), px=float_str(buy_price, tick_decimals),
                             sz=float_str(buy_size, size_decimals))
                orders.append(order)
                grid = dict(index=index, price=buy_price, order=order)
                grids.append(grid)
                index -= 1
                buy_price = round_to(buy_price * b ** 2, tick_size)
                buy_size = round_to(buy_size * a, size_increment)
            grids.sort(key=lambda x: x['index'])

            if orders:
                orders = await self.tradeAPI.batch_order(orders)
                for order in orders:
                    assert order['sCode'] == '0', print(order)
            else:
                sell_size = round_to(ni * (1 - b), size_increment)
                buy_size = round_to(ni * (a - 1) / (1 + maker_fee), size_increment)
                print(f'{sell_size >= min_size=}, {buy_size >= min_size=}')
                print('Use larger grid.')

        await init_grids()
        index = grid_num // 2

        async def move_grids():
            nonlocal grids, ni, last
            grids = []
            await cancel_orders()
            ni = n1
            last = k / n1 ** 2
            await init_grids()

        kwargs = OKExAPI._key()
        kwargs['channels'] = [dict(channel='orders', instType='SPOT', instId=self.spot_ID)]

        try:
            async for current_order in subscribe(self.private_url, **kwargs):
                current_order = current_order['data'][0]
                timestamp = utcfrommillisecs(current_order['uTime'])
                print(datetime_str(utc_to_local(timestamp)), current_order['clOrdId'], current_order['side'],
                      current_order['px'], current_order['state'])
                # 下单成功
                if current_order['state'] == 'filled':
                    index = current_order['clOrdId'][current_order['clOrdId'].find('grid') + 4:]
                    index = int(index)
                    n1 = ni * pow(b, index - grid_num // 2)
                    spot_price = float(current_order['avgPx'])
                    spot_fee = float(current_order['fee'])
                    if current_order['side'] == 'sell':
                        spot_filled = float(current_order['accFillSz'])
                        cash_notional = spot_filled * spot_price
                        spot_notional = k * spot_filled / n1 / (n1 + spot_filled)
                        mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp,
                                      op='sell', cash_notional=cash_notional, spot_notional=spot_notional,
                                      fee=spot_fee, k=k)
                        Record.mycol.insert_one(mydict)

                        if abs(index - grid_num // 2) == grid_num // 2:
                            await move_grids()
                        else:
                            buy_price = round_to(k / (n1 * a) ** 2, tick_size)
                            buy_size = round_to(n1 * (a - 1) / (1 + maker_fee), size_increment)
                            buy_price = float_str(buy_price, tick_decimals)
                            buy_size = float_str(buy_size, size_decimals)
                            kwargs = dict(instId=self.spot_ID, side='buy', size=buy_size, price=buy_price,
                                          order_type='limit', client_oid=self.coin + 'grid' + str(index - 1))
                            buy_order = create_task(self.tradeAPI.take_spot_order(**kwargs))
                            buy_order = await buy_order
                            assert buy_order['ordId'] != '-1', print(buy_order)
                    elif current_order['side'] == 'buy':
                        spot_filled = float(current_order['accFillSz']) + spot_fee
                        cash_notional = - spot_filled * spot_price
                        spot_notional = - k * spot_filled / n1 / (n1 - spot_filled)
                        mydict = dict(account=self.accountid, instrument=self.coin, timestamp=timestamp,
                                      op='buy', cash_notional=cash_notional, spot_notional=spot_notional,
                                      fee=spot_fee, k=k)
                        Record.mycol.insert_one(mydict)

                        if abs(index - grid_num // 2) == grid_num // 2:
                            await move_grids()
                        else:
                            sell_price = round_to(k / (n1 * b) ** 2, tick_size)
                            sell_price = float_str(sell_price, tick_decimals)
                            sell_size = round_to(n1 * (1 - b), size_increment)
                            sell_size = float_str(sell_size, size_decimals)
                            kwargs = dict(instId=self.spot_ID, side='sell', size=sell_size, price=sell_price,
                                          order_type='limit', client_oid=self.coin + 'grid' + str(index + 1))
                            sell_order = create_task(self.tradeAPI.take_spot_order(**kwargs))
                            sell_order = await sell_order
                            assert sell_order['ordId'] != '-1', print(sell_order)
                    else:
                        print(current_order)

                    stat()

        except asyncio.CancelledError:
            print(datetime_str(datetime.now()))
            n1 = ni * pow(b, index - grid_num // 2)
            price = round_to(k / n1 ** 2, tick_size)
            print(f'{index=}, {n1=:.{size_decimals}f}, {price=:.{tick_decimals}f}')
            orders = []
            for grid in grids:
                if grid['order']:
                    orders.append(
                        self.tradeAPI.get_order_info(instId=self.spot_ID, client_oid=grid['order']['clOrdId']))
            assert len(orders) <= 60
            orders = await gather(*orders)
            for order in orders:
                if order['state'] != 'live':
                    index = order['clOrdId'][order['clOrdId'].find('grid') + 4:]
                    index = int(index)
                    n1 = ni * pow(b, index - grid_num // 2)
                    price = round_to(k / n1 ** 2, tick_size)
                    print(f"{index=}, {n1=:.{size_decimals}f}, {price=:.{tick_decimals}f}', "
                          f"{order['side']=}, {order['state']=}")
            await cancel_orders()
