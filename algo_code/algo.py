import pandas as pd
from typing import Optional, List, Union, Literal

from algo_code.datatypes import Pivot, Candle
from algo_code.general_utils import make_set_width
from algo_code.order_block import OrderBlock
from algo_code.segment import Segment
from algo_code.position import Position
import utils.constants as constants
from utils.logger import logger


class Algo:
    def __init__(self, pair_df: pd.DataFrame,
                 symbol: str):

        self.pair_df: pd.DataFrame = pair_df
        self.symbol: str = symbol
        self.zigzag_df: Optional[pd.DataFrame] = None

        # pbos_indices and choch_indices is a list which stores the PBOS and CHOCH's being moved due to shadows breaking the most recent lows/highs
        self.pbos_indices: list[int] = []
        self.choch_indices: list[int] = []

        # Indices of NON-BROKEN LPL's. This means only LPL's that get updated to the next one in the calc_broken_lpl method are added here.
        self.lpl_indices: dict[str, list] = {
            "peak": [],
            "valley": []
        }

        # h_o_indices indicates the indices of the peaks and valleys in the higher order zigzag
        self.h_o_indices: list[int] = []

        # This is a list that will contain all the segments to be processed in the final backtest. Each segment is a datatype with its start and end
        # PDI's specified, and a top and bottom price for plotting purposes. Segments end at PBOS_CLOSE events and start at the low/high before the
        # PBOS which they closed above/below (Ascending/Descending)
        self.segments: list[Segment] = []

        # starting_pdi is the starting point of the entire pattern, calculated using __init_pattern_start_pdi. This method is
        # executed in the calc_h_o_zigzag method.
        self.starting_pdi = 0

    def init_zigzag(self, last_pivot_type=None, last_pivot_candle_pdi=None) -> None:
        """
            Method to identify turning points in a candlestick chart.
            It compares each candle to its previous pivot to determine if it's a new pivot point.
            This implementation is less optimized than the deprecated version, as it doesn't use
            vectorized operations, but it is what it is

            Returns:
            pd.DataFrame: A DataFrame containing the identified turning points.
            """

        if last_pivot_type is None:
            # Find the first candle that has a higher high or a lower low than its previous candle
            # and set it as the first pivot. Also set the type of the pivot (peak or valley)

            last_pivot_candle_series = \
                self.pair_df[(self.pair_df['high'] > self.pair_df['high'].shift(1)) | (
                        self.pair_df['low'] < self.pair_df['low'].shift(1))].iloc[0]

            last_pivot_type: str = 'valley'
            if last_pivot_candle_series.high > self.pair_df.iloc[last_pivot_candle_series.name - 1].high:
                last_pivot_type = 'peak'

        # If a first candle is already given
        else:
            last_pivot_candle_series = self.pair_df.loc[last_pivot_candle_pdi]

        last_pivot_candle: Candle = Candle.create(last_pivot_candle_series)
        pivots: List[Pivot] = []

        # Start at the candle right after the last (first) pivot
        for row in self.pair_df.iloc[last_pivot_candle.pdi + 1:].itertuples():

            # Conditions to check if the current candle is an extension of the last pivot or a reversal
            peak_extension_condition: bool = row.high > last_pivot_candle.high and last_pivot_type == 'peak'
            valley_extension_condition: bool = row.low < last_pivot_candle.low and last_pivot_type == 'valley'

            reversal_from_peak_condition = row.low < last_pivot_candle.low and last_pivot_type == 'peak'
            reversal_from_valley_condition = row.high > last_pivot_candle.high and last_pivot_type == 'valley'

            # Does the candle register both a higher high AND a lower low?
            if (reversal_from_valley_condition and valley_extension_condition) or (
                    peak_extension_condition and reversal_from_peak_condition):

                # INITIAL NAIVE IMPLEMENTATION
                # Add the last previous pivot to the list
                # pivots.append(Pivot.create((last_pivot_candle, last_pivot_type)))

                # Update the last pivot's type and value
                # last_pivot_candle = Candle.create(row)
                # last_pivot_type = 'valley' if last_pivot_type == 'peak' else 'peak'

                # JUDGING BASED ON CANDLE COLOR
                # If the candle is green, that means the low value was probably hit before the high value
                # If the candle is red, that means the high value was probably hit before the low value
                # This means that if the candle is green, we can extend a peak, and if it's red, we can extend a valley
                # Otherwise the direction must flip
                if (row.candle_color == 'green' and last_pivot_type == 'valley') or (
                        row.candle_color == 'red' and last_pivot_type == 'peak'):
                    # Add the last previous pivot to the list of pivots
                    pivots.append(Pivot.create((last_pivot_candle, last_pivot_type)))

                    # Update the last pivot's type and value
                    last_pivot_candle = Candle.create(row)
                    last_pivot_type = 'valley' if last_pivot_type == 'peak' else 'peak'

                else:
                    last_pivot_candle = Candle.create(row)

            # Has a same direction pivot been found?
            if peak_extension_condition or valley_extension_condition:
                # Don't change the direction of the last pivot found, just update its value
                last_pivot_candle = Candle.create(row)

            # Has a pivot in the opposite direction been found?
            elif reversal_from_valley_condition or reversal_from_peak_condition:
                # Add the last previous pivot to the list of pivots
                pivots.append(Pivot.create((last_pivot_candle, last_pivot_type)))

                # Update the last pivot's type and value
                last_pivot_candle = Candle.create(row)
                last_pivot_type = 'valley' if last_pivot_type == 'peak' else 'peak'

        # Convert the pivot list to zigzag_df
        # noinspection PyTypeChecker
        zigzag_df = pd.DataFrame.from_dict(pivot._asdict() for pivot in pivots)

        self.zigzag_df = zigzag_df

    def find_relative_pivot(self, pivot_pdi: int, delta: int) -> int:
        """
        Finds the relative pivot to the pivot at the given index.

        Args:
            pivot_pdi (int): The pdi of the pivot to find the relative pivot for.
            delta (int): The distance from the pivot to the relative pivot.

        Returns:
            int: The pdi of the relative pivot.
        """

        # zigzag_idx is the zigzag_df index of the current pivot
        zigzag_idx = self.zigzag_df[self.zigzag_df.pdi == pivot_pdi].first_valid_index()

        return self.zigzag_df.iloc[zigzag_idx + delta].pdi

    def detect_first_broken_lpl(self, search_window_start_pdi: int) -> Union[None, tuple[pd.Series, int]]:
        """
        Calculates the LPL's and then broken LPL's in a series of zigzag pivots.


        An LPL (For ascending patterns) is registered when a higher high than the highest high since the last LPL is registered. If a lower low than
        the lowest low is registered, the last LPL is considered a broken LPL and registered as such.

        Args:
            search_window_start_pdi (int): The pdi of the pivot to start the search from.

        Returns:
            pd.Series: a row of zigzag_df which contains the broken LPL
            None: If no broke LPL is found
        """

        starting_pivot = self.zigzag_df[self.zigzag_df.pdi == search_window_start_pdi].iloc[0]
        trend_type = "ascending" if starting_pivot.pivot_type == "valley" else "descending"
        # Breaking and extension pdi and values represent the values to surpass for registering a higher high (extension) of a lower low (breaking)
        breaking_pdi = search_window_start_pdi
        breaking_value: float = starting_pivot.pivot_value

        try:
            extension_pdi = self.find_relative_pivot(search_window_start_pdi, 1)

            extension_value: float = self.zigzag_df.loc[self.zigzag_df.pdi == extension_pdi].iloc[0].pivot_value

            check_start_pdi = self.find_relative_pivot(search_window_start_pdi, 2)

        # If a next pivot isn't found, that means no breaking has occurred.
        except IndexError:
            return None

        for row in self.zigzag_df[self.zigzag_df.pdi >= check_start_pdi].iloc[:-1].itertuples():
            if trend_type == "ascending":
                extension_condition = row.pivot_type == "peak" and row.pivot_value >= extension_value
                breaking_condition = row.pivot_type == "valley" and row.pivot_value <= breaking_value
            else:
                extension_condition = row.pivot_type == "valley" and row.pivot_value <= extension_value
                breaking_condition = row.pivot_type == "peak" and row.pivot_value >= breaking_value

            # Breaking
            if breaking_condition:
                # If a breaking event has occurred, we need to find the actual CANDLE that broke the LPL, since it might have happened before the
                # PIVOT that broke the LPL, since zigzag pivots are a much more aggregated type of data compared to the candles and almost always
                # the actual candle that breaks the LPL is one of the candles before the pivot that was just found.

                # The candle search range starts at the pivot before the LPL-breaking pivot (which is typically a higher order pivot) PDI and the
                # breaking pivot PDI.
                pivot_before_breaking_pivot: int = self.find_relative_pivot(row.pdi, -1)
                breaking_candle_search_window: pd.DataFrame = self.pair_df.loc[pivot_before_breaking_pivot + 1:row.pdi + 1]

                # If the trend is ascending, it means the search window should be checked for the first candle that breaks the LPL by having a lower
                # low than the breaking_value.
                if trend_type == "ascending":
                    lpl_breaking_candles = breaking_candle_search_window[breaking_candle_search_window.low < breaking_value]

                # If the trend is descending, the breaking candle must have a higher high than the breaking value.
                else:
                    lpl_breaking_candles = breaking_candle_search_window[breaking_candle_search_window.high > breaking_value]

                breaking_candle_pdi = lpl_breaking_candles.first_valid_index()

                # If the search window for the breaking candle is empty, return the pivot as the breaking candle
                if breaking_candle_pdi is None:
                    breaking_candle_pdi = row.pdi

                return self.zigzag_df[self.zigzag_df.pdi == breaking_pdi].iloc[0], breaking_candle_pdi

            # Extension
            if extension_condition:
                # If a higher high is found, extend and update the pattern

                prev_pivot_pdi = self.find_relative_pivot(row.pdi, -1)
                prev_pivot = self.zigzag_df[self.zigzag_df.pdi == prev_pivot_pdi].iloc[0]

                breaking_pdi = prev_pivot.pdi
                breaking_value = prev_pivot.pivot_value
                extension_value = row.pivot_value

            # If a break or extension has happened, the next LPL is the pivot at the breaking pivot
            if breaking_condition or extension_condition:
                if trend_type == "ascending":
                    self.lpl_indices["valley"].append(breaking_pdi)
                else:
                    self.lpl_indices["peak"].append(breaking_pdi)

        return None

    def __detect_breaking_sentiment(self, latest_pbos_value: float, latest_pbos_pdi: int, latest_choch_value: float,
                                    trend_type: str) -> dict:
        """
        Detects the breaking sentiment in the price data based on the latest PBOS and CHOCH values.

        This method identifies the candles that break the PBOS and CHOCH values either by shadow or close price. It then determines
        which breaking event occurs first and returns the sentiment associated with that event.

        Args:
            latest_pbos_value (float): The latest PBOS value.
            latest_pbos_pdi (int): The index of the latest PBOS.
            latest_choch_value (float): The latest CHOCH value.
            trend_type (str): The current trend type, either "ascending" or "descending".

        Returns:
            dict: A dictionary containing the breaking sentiment and the index of the candle that caused the break. The breaking sentiment
                  can be one of the following: "PBOS_SHADOW", "PBOS_CLOSE", "CHOCH_SHADOW", "CHOCH_CLOSE", "NONE".
        """

        # We only go up to the second last candle in the pair_df, aka the last non-realtime candle, because otherwise, the close value of the candle
        # might change and incorrectly register a break.
        search_window: pd.DataFrame = self.pair_df.iloc[latest_pbos_pdi + 1:-1]

        # The definition of "breaking" is different whether the PBOS is a peak or a valley
        if trend_type == "ascending":
            pbos_shadow_breaking_candles = search_window[search_window.high > latest_pbos_value]
            pbos_close_breaking_candles = search_window[search_window.close > latest_pbos_value]
            choch_shadow_breaking_candles = search_window[search_window.low < latest_choch_value]
            choch_close_breaking_candles = search_window[search_window.close < latest_choch_value]

        else:
            pbos_shadow_breaking_candles = search_window[search_window.low < latest_pbos_value]
            pbos_close_breaking_candles = search_window[search_window.close < latest_pbos_value]
            choch_shadow_breaking_candles = search_window[search_window.high > latest_choch_value]
            choch_close_breaking_candles = search_window[search_window.close > latest_choch_value]

        pbos_close_index = pbos_close_breaking_candles.first_valid_index()
        pbos_shadow_index = pbos_shadow_breaking_candles.first_valid_index()
        choch_shadow_index = choch_shadow_breaking_candles.first_valid_index()
        choch_close_index = choch_close_breaking_candles.first_valid_index()

        # The return dicts for each case
        pbos_shadow_output = {
            "sentiment": "PBOS_SHADOW",
            "pdi": pbos_shadow_index
        }
        pbos_close_output = {
            "sentiment": "PBOS_CLOSE",
            "pdi": pbos_close_index
        }
        choch_shadow_output = {
            "sentiment": "CHOCH_SHADOW",
            "pdi": choch_shadow_index
        }
        choch_close_output = {
            "sentiment": "CHOCH_CLOSE",
            "pdi": choch_close_index
        }
        none_output = {
            "sentiment": "NONE",
            "pdi": None
        }

        outputs_list: list[dict] = [pbos_shadow_output, pbos_close_output, choch_shadow_output, choch_close_output]

        # This function sorts the outputs of the breaking sentiment analysis to determine which one is reached first using the
        # sorted built-in function. It also prioritizes sentiments that have "CLOSE" in their description, because a candle closing above/below a
        # value logically takes priority over a shadow.
        def sorting_key(output_item):
            pdi = output_item["pdi"] if output_item["pdi"] is not None else 0
            has_close = 1 if "CLOSE" in output_item["sentiment"] else 2
            return pdi, has_close

        sorted_outputs: list[dict] = [output_item for output_item in sorted(outputs_list, key=sorting_key)
                                      if output_item["pdi"] is not None]

        return sorted_outputs[0] if len(sorted_outputs) > 0 else none_output

    def __calc_region_start_pdi(self, broken_lpl: pd.Series) -> int:
        """
        Initializes the starting point of the region after the broken LPL

        The region starting point is the first pivot right after the broken LPL

        Args:
            broken_lpl (pd.Series): The broken LPL
        """

        # The pivots located between the starting point and the first pivot after the broken LPL. The starting point is either
        # 1) The start of the pattern, which means we are forming the first region, or
        # 2) The start of the next section. The region_start_pdi variable determines this value.
        region_start_pdi = self.find_relative_pivot(broken_lpl.pdi, 1)

        return region_start_pdi

    def calc_h_o_zigzag(self, starting_point_pdi) -> None:
        """
        Calculates the higher order zigzag for the given starting point.

        This method sets the starting point of the higher order zigzag and adds it to the list of higher order indices. It then
        identifies the first broken LPL after the starting point and determines the trend type based on the type of the broken LPL.
        It then identifies the base of the swing (BOS) which is the pivot right after the broken LPL.

        The method then enters a loop where it checks for breaking sentiments (either PBOS_SHADOW, CHOCH_SHADOW, PBOS_CLOSE, CHOCH_CLOSE or NONE)
        and updates the latest PBOS and CHOCH thresholds accordingly if a shadow has broken a PBOS or CHOCH. If a PBOS_CLOSE or CHOCH_CLOSE sentiment
        is detected, the method identifies the extremum point and adds it to the higher order indices, and then resets the starting point for finding
        higher order pivots.

        Any PBOS_CLOSE events will trigger a segment creation. The segment is then added to the list of segments. A segment is a region within which
        the order blocks aren't invalidated. This means that the trades can be safely entered in each segment independently without worrying about
        OB updates.

        The loop continues until no more candles are found that break the PBOS or CHOCH even with a shadow.

        Args:
            starting_point_pdi (int): The starting point of the higher order zigzag.

        Returns:
            None
        """

        # Set the starting point of the HO zigzag and add it
        self.starting_pdi = starting_point_pdi
        self.h_o_indices.append(self.starting_pdi)

        # The first CHOCH is always the starting point, until it is updated when a BOS or a CHOCH is broken.
        latest_choch_threshold: float = self.zigzag_df[self.zigzag_df.pdi == self.starting_pdi].iloc[0].pivot_value

        # The starting point of each pattern. This resets and changes whenever the pattern needs to be restarted. Unlike self.starting_pdi this DOES
        # change.
        pattern_start_pdi = self.starting_pdi

        latest_pbos_pdi = None
        latest_pbos_threshold = None

        # The loop which continues until the end of the pattern is reached.
        while True:
            # Find the first broken LPL after the starting point and the region starting point
            broken_lpl_output_set = self.detect_first_broken_lpl(pattern_start_pdi)

            # If no broken LPL can be found, just quit
            if broken_lpl_output_set is None:
                break

            else:
                broken_lpl = broken_lpl_output_set[0]
                lpl_breaking_pdi: int = broken_lpl_output_set[1]

            # If the LPL type is valley, it means the trend type is ascending
            trend_type = "ascending" if broken_lpl.pivot_type == "valley" else "descending"

            # The BOS is the pivot right after the broken LPL
            bos_pdi = int(self.__calc_region_start_pdi(broken_lpl))

            # When pattern resets, aka a new point is found OR when the pattern is initializing. Each time a restart is required in the next
            # iteration, latest_pbos_pdi is set to None.
            if latest_pbos_pdi is None:
                latest_pbos_pdi = bos_pdi
                latest_pbos_threshold = self.zigzag_df[self.zigzag_df.pdi == bos_pdi].iloc[0].pivot_value

                # Add the BOS to the HO indices
                self.h_o_indices.append(bos_pdi)

            # Add the first found PBOS to the list as that is needed to kickstart the h_o_zigzag
            self.pbos_indices.append(bos_pdi)

            # If the candle breaks the PBOS by its shadow, the most recent BOS threshold will be moved to that candle's high instead
            # If a candle breaks the PBOS with its close value, then the search halts
            # If the candle breaks the last CHOCH by its shadow, the CHOCH threshold will be moved to that candle's low
            # If a candle breaks the last CHOCH with its close, the direction inverts and the search halts
            # These sentiments are detected using the self.__detect_breaking_sentiment method.
            breaking_output = self.__detect_breaking_sentiment(latest_pbos_threshold, latest_pbos_pdi,
                                                               latest_choch_threshold, trend_type)
            breaking_pdi = breaking_output["pdi"]
            breaking_sentiment = breaking_output["sentiment"]

            # For brevity and simplicity, from this point on all the comments are made with the ascending pattern in mind. THe descending pattern is
            # exactly the same, just inverted.
            # If a PBOS has been broken by a shadow(And ONLY its shadow, not its close value. This is explicitly enforced in the sentiment detection
            # method, where CLOSE sentiments are given priority over SHADOW ), update the latest PBOS pdi and threshold (level). Note that since this
            # statement doesn't set latest_pbos_pdi to None, the pattern will not restart.
            if breaking_sentiment == "PBOS_SHADOW":

                latest_pbos_pdi = breaking_pdi
                latest_pbos_threshold = self.pair_df.iloc[breaking_pdi].high if trend_type == "ascending" else \
                    self.pair_df.iloc[breaking_pdi].low

            # If a candle breaks the CHOCH with its shadow (And ONLY its shadow, not its close value), update the latest CHOCH pdi and threshold
            elif breaking_sentiment == "CHOCH_SHADOW":
                latest_choch_threshold = self.pair_df.iloc[breaking_pdi].low if trend_type == "ascending" else \
                    self.pair_df.iloc[breaking_pdi].high

            # If a candle CLOSES above the latest PBOS value, it means we have found an extremum, which would be the lowest low zigzag pivot between
            # the latest HO zigzag point (The initial BOS before being updated with shadows) and the candle which closed above it. After detecting
            # this extremum, we add it to HO Zigzag.
            elif breaking_sentiment == "PBOS_CLOSE":
                # The extremum point is the point found using a "lowest low" of a "highest high" search between the last HO pivot and
                # the closing candle
                extremum_point_pivot_type = "valley" if trend_type == "ascending" else "peak"

                # extremum_point_pivots_of_type is a list of all the pivots of the right type for the extremum
                extremum_point_pivots_of_type = self.zigzag_df[
                    (self.zigzag_df.pdi >= self.h_o_indices[-1])
                    & (self.zigzag_df.pdi <= breaking_pdi)
                    & (self.zigzag_df.pivot_type == extremum_point_pivot_type)]

                # The extremum pivot is the lowest low / the highest high in the region between the first PBOS and the closing candle
                if extremum_point_pivot_type == "peak":
                    extremum_pivot = extremum_point_pivots_of_type.loc[
                        extremum_point_pivots_of_type['pivot_value'].idxmax()]
                else:
                    extremum_pivot = extremum_point_pivots_of_type.loc[
                        extremum_point_pivots_of_type['pivot_value'].idxmin()]

                # Add the extremum point to the HO indices
                self.h_o_indices.append(int(extremum_pivot.pdi))

                # Now, we can restart finding HO pivots. Starting point is set to the last LPL of the same type BEFORE the BOS breaking candle.
                # Trend stays the same since no CHOCH has occurred.
                pivot_type = "valley" if trend_type == "ascending" else "peak"
                pivots_of_type_before_closing_candle = self.zigzag_df[(self.zigzag_df.pivot_type == pivot_type)
                                                                      & (self.zigzag_df.pdi <= breaking_pdi)]

                pattern_start_pdi = pivots_of_type_before_closing_candle.iloc[-1].pdi

                # Essentially reset the algorithm
                latest_pbos_pdi = None

                # A segment is added to the list of segments here. Each segment starts at the pivot before the high that was just broken by a candle
                # closing above it. The segment ends at the PBOS_CLOSE event, at the candle that closed above the high. The -3 index is used because
                # there are two points after it: The high that was just broken, and the extremum that was added because the high was broken; therefore
                # we need the THIRD to last pivot.
                segment_to_add: Segment = Segment(start_pdi=self.h_o_indices[-3],
                                                  end_pdi=breaking_pdi - 1,
                                                  ob_leg_start_pdi=self.h_o_indices[-3],
                                                  ob_leg_end_pdi=self.h_o_indices[-2],
                                                  top_price=latest_pbos_threshold,
                                                  bottom_price=latest_choch_threshold,
                                                  ob_formation_start_pdi=lpl_breaking_pdi + 1,
                                                  broken_lpl_pdi=broken_lpl.pdi,
                                                  type=trend_type)
                self.segments.append(segment_to_add)

                # New lowest low is our CHOCH.
                latest_choch_pdi = self.h_o_indices[-1]
                latest_choch_threshold = self.zigzag_df[self.zigzag_df.pdi == latest_choch_pdi].iloc[0].pivot_value

            # If a CHOCH has happened, this means the pattern has inverted and should be restarted with the last LPL before the candle which closed
            # below the CHOCH.
            elif breaking_sentiment == "CHOCH_CLOSE":

                trend_type = "ascending" if trend_type == "descending" else "descending"

                # Set the pattern start to the last inverse pivot BEFORE the closing candle
                pivot_type = "valley" if trend_type == "ascending" else "peak"
                pivots_of_type_before_closing_candle = self.zigzag_df[(self.zigzag_df.pivot_type == pivot_type)
                                                                      & (self.zigzag_df.pdi <= breaking_pdi)]

                pattern_start_pdi = pivots_of_type_before_closing_candle.iloc[-1].pdi

                # A segment is added to the list of segments here. Each segment starts at the pivot before the low that was just broken by a candle
                # closing below it. The segment ends at the CHOCH_CLOSE event, at the candle that closed above the high.
                # we need the THIRD to last pivot. trend_type needs to be reverted because we are still working on the same positions from before
                # the CHOCH happened and in the same direction, just that the event is different...
                segment_to_add: Segment = Segment(start_pdi=self.h_o_indices[-2],
                                                  end_pdi=breaking_pdi,
                                                  ob_leg_start_pdi=self.h_o_indices[-2],
                                                  ob_leg_end_pdi=self.h_o_indices[-2],
                                                  top_price=latest_pbos_threshold,
                                                  bottom_price=latest_choch_threshold,
                                                  ob_formation_start_pdi=lpl_breaking_pdi + 1,
                                                  broken_lpl_pdi=broken_lpl.pdi,
                                                  type="ascending" if trend_type == "descending" else "descending", formation_method="choch")
                self.segments.append(segment_to_add)

                # Essentially reset the algorithm
                latest_choch_pdi = self.h_o_indices[-1]
                latest_choch_threshold = self.zigzag_df[self.zigzag_df.pdi == latest_choch_pdi].iloc[0].pivot_value

                latest_pbos_pdi = None

            # If no candles have broken the PBOS even with a shadow, break the loop
            else:
                break

        # return self.h_o_indices

    def convert_pdis_to_times(self, pdis: Union[int, list[int]]) -> Union[pd.Timestamp, list[pd.Timestamp], None]:
        """
        Convert a list (or a single) of PDIs to their corresponding times using algo_code.pair_df.

        Args:
            pdis (list[int]): List of PDIs to convert.

        Returns:
            list[pd.Timestamp]: List of corresponding times.
        """

        if pdis is None:
            return None

        if not isinstance(pdis, list):
            pdis = [pdis]

        if len(pdis) == 0:
            return []

        # Map PDIs to their corresponding times
        times = [self.pair_df.iloc[pdi].time for pdi in pdis]

        # If it's a singular entry, return it as a single timestamp
        if len(times) == 1:
            return times[0]

        return list(times)

    # _____________________________________________________________________________________________
    # Forward-test specific utilities
    def find_position_search_window(self, latest_segment: Segment) -> Union[None, dict[str, int]]:
        """
        This method will use the direction and the formation type of the latest segment found in the pattern to determine the search window for the
        positions to be posted.

        If the latest segment has a BOS formation type, that means the direction of the trend has not changed, and we can
        continue with the positions in the same direction. The search window will be from the second-to-last higher order pivot to the first broken
        LPL after the last segment, aka on the last-found-leg of the higher order zigzag, up to the broken LPL. The position type will be determined
        based on the trend direction.
        If the latest segment was formed with a CHOCH break, that means the direction is now reversed. The algorithm will try to find positions
        starting from the last higher order pivot to the first broken LPL after the last segment. The position type will be determined based on the
        direction of the latest segment, as in it will be the reverse of that direction.

        The same checks and conditions as the order-block-finding algorithm in the segments will be applied, with one
        caveat: The condition and reentry check window won't be limited to the breaking LPL, aka the conditions will not just be checked for the
        period of the last higher order leg, instead the upper bound of the condition check window will be the last fond candle. This is especially
        important in the reentry check, since an order block which has already been entered, even after the formation check window, would be invalid
        for posting in the channel, since the entry has already been made. This may later be overridden by introducing "bounces" for each order block,
        similar to ST2.

        Args:
            latest_segment (Segment): The latest segment found by the algorithm, which determines where the position-forming HO zigzag leg will be
                                      located.

        Returns:
            None: The method returns None if no broken/breaking LPL is found. This would mean that the higher order leg hasn't formed yet. This should
                  logically only happen in the case of a CHOCH formation for the latest segment, since with a BOS formation the leg has already formed
                  so the broken LPL already exists.

            Dict: A dict containing the PDI's of the search window's start and end, and the PDI of the activation threshold, keys "start", "end",
                  "activation_threshold".

        """

        # If the latest segment was formed from a BOS break, that means the direction hasn't changed. So the latest found leg (of the same direction)
        # is still valid. The start of the position search would be the second-to-last higher order zigzag pivot, and the end of it would be the LPL
        # that is broken.
        if latest_segment.formation_method == "bos":
            position_search_start_pdi: int = self.h_o_indices[-2]

            # The pivot types we need are linked to the trend direction, which in the case of a BOS formation type, would be in the same as the latest
            # segment. We need the correct pivot type to use the detect_first_broken_lpl method correctly.
            pivot_type = "valley" if latest_segment.type == "ascending" else "peak"
            pivots_of_type_before_closing_candle = self.zigzag_df[(self.zigzag_df.pivot_type == pivot_type)
                                                                  & (self.zigzag_df.pdi <= latest_segment.end_pdi)]

            # The detect_first_broken_lpl method returns two things as a tuple: 1) The LPL that was broken 2) The PDI of the candle that broke the
            # LPL.
            broken_lpl_data = self.detect_first_broken_lpl(pivots_of_type_before_closing_candle.iloc[-1].pdi)

            # The end of the search window is set as the first broken LPL AFTER the LAST LOW before the end of the last segment.
            # If the detect_first_broken_lpl method returns a value, that means a broken LPL has been found. If not, the method returns None,
            # signaling that no broken LPL was found. In the case of a BOS formation type, this wouldn't normally happen.
            if broken_lpl_data:
                position_search_end_pdi: int = broken_lpl_data[0].pdi

                # The positions should only be activated after the LPL has been broken by a candle.
                position_activation_threshold: int = broken_lpl_data[1]

                return {
                    "start": position_search_start_pdi,
                    "end": position_search_end_pdi,
                    "activation_threshold": position_activation_threshold
                }

            else:
                return None

        # In the case of a CHOCH formation in the latest segment, the direction has changed. The search window will be from the last higher order
        # zigzag pivot to the first broken LPL after the last segment. The position type will be the reverse of the latest segment's direction. All
        # other details are the same as a BOS formation.
        else:
            position_search_start_pdi: int = self.h_o_indices[-1]

            pivot_type = "peak" if latest_segment.type == "ascending" else "valley"
            pivots_of_type_before_closing_candle = self.zigzag_df[(self.zigzag_df.pivot_type == pivot_type)
                                                                  & (self.zigzag_df.pdi <= latest_segment.end_pdi)]
            broken_lpl_data = self.detect_first_broken_lpl(pivots_of_type_before_closing_candle.iloc[-1].pdi)

            if broken_lpl_data:
                position_search_end_pdi: int = broken_lpl_data[0].pdi

                # The positions should only be activated after the LPL has been broken by a candle.
                position_activation_threshold: int = broken_lpl_data[1]

                return {
                    "start": position_search_start_pdi,
                    "end": position_search_end_pdi,
                    "activation_threshold": position_activation_threshold
                }

            else:
                return None

    def determine_main_loop_start_type(self, pair_name: str, positions_info_dict) -> Literal["NO_NEW_SEGMENT", "RESET_POSITIONS"]:
        """
        Resets the algorithm from the beginning in two cases: 1) If the segment that the most recent positions are in ends 2) If no latest segment has
        been defined. Case 1 happens whe a BOS or CHOCH break is completed, and therefore the positions are invalidated. Case 2 happens when the
        algorithm is starting totally fresh and no latest segment has been registered.

        Returns:
            None: The method directly modifies the positions_info_dict object and operates on its children.
        """

        # If the latest segment's start time is newer than the start time of the segment registered when the positions were found (Or if we are
        # starting fresh with no latest_segment registered), that means the old segment has been invalidated by a new segment forming.
        # In this case, the existing positions (if any) should be canceled and new ones should be posted or awaited.
        is_starting_fresh: bool = positions_info_dict[pair_name]["latest_segment_start_time"] is None
        is_new_segment_found: bool = is_starting_fresh or self.convert_pdis_to_times(self.segments[-1].start_pdi) > positions_info_dict[pair_name][
            "latest_segment_start_time"]

        # If we are not in a new segment, and we aren't starting with no positions, we can skip the rest of the code for this pair.
        if not is_new_segment_found and not is_starting_fresh:
            return "NO_NEW_SEGMENT"

        # If there is a new segment, the rest of the code will execute, but also the positions found in the previous segment will be canceled.
        else:
            if is_starting_fresh:
                logger.info(f"\t{make_set_width(pair_name)}\tNo prior latest segment history, starting fresh...")

            elif is_new_segment_found:
                logger.info(f"\t{make_set_width(pair_name)}\tNew segment found, canceling prior positions...")

                for position in positions_info_dict[pair_name]["positions"]:
                    attempts = 0
                    while attempts < 3:
                        try:
                            position.cancel_position()

                            logger.warning(f"\t{make_set_width(pair_name)}\tCanceled position {position.parent_ob.id}...")
                            break

                        except RuntimeError:
                            logger.warning(
                                f"\t{make_set_width(pair_name)}\tPosition {position.parent_ob.id} wasn't canceled as it had been entered before.")
                            break

                        except Exception as e:
                            attempts += 1
                            if attempts == 3:
                                logger.error(
                                    f"\t{make_set_width(pair_name)}\tFailed to cancel position {position.parent_ob.id} after 3 attempts: {e}")

            # Empty the list of positions, so we can wait for a new one.
            positions_info_dict[pair_name]["positions"] = []

            positions_info_dict[pair_name]["has_been_searched"] = False
            positions_info_dict[pair_name]["last_log_message"] = "CANCELED_POSITIONS"

            # Regardless o whether any positions are found or not in the future lines, we need to register the latest segment.
            positions_info_dict[pair_name]["latest_segment_start_time"] = self.convert_pdis_to_times(self.segments[-1].start_pdi)

            logger.debug(
                f"\t{make_set_width(pair_name)}\tLatest segment start time registered: {positions_info_dict[pair_name]['latest_segment_start_time']}")

            return "RESET_POSITIONS"

    def define_replacement_ob_threshold(self, pivot: pd.Series) -> int:
        """
        Form a window of candles to check for replacement order blocks. This window is bound by the current pivot and the next pivot of
        opposite type, hence the pivot and the pivot found by shifting it by 1. This is a naive implementation, and under normal
        circumstances we don't need to check that far.

        Args:
            pivot (pd.Series): A lower order zigzag pivot, located at the start (Or the "tip") of a lower order leg.

        Returns:
            int: The PDI of the last candle that should be checked for a replacement OB base candle.
        """

        try:
            replacement_ob_threshold_pdi = self.find_relative_pivot(pivot.pdi, 1)
        except IndexError:
            # If no next pivot exists for whatever reason, just set the threshold to the last valid index of the dataframe.
            replacement_ob_threshold_pdi = self.pair_df.last_valid_index()

        return replacement_ob_threshold_pdi

    def form_potential_ob(self,
                          base_candle: pd.Series,
                          base_pivot_type: str,
                          initial_pivot_candle_liquidity: float,
                          position_activation_threshold: int) -> OrderBlock | None:
        """
            Forms a potential order block (OB) based on the given base candle and conditions.

            Args:
                base_candle (pd.Series): The base candle to form the order block.
                base_pivot_type (str): The type of the base pivot, either "valley" or "peak".
                initial_pivot_candle_liquidity (float): The liquidity of the pivot candle, used to determine the stoploss level.
                position_activation_threshold (int): The PDI of the candle after which the positions are activated.

            Returns:
                OrderBlock | None: The formed order block after checking the conditions, otherwise None if no exit candle is found.
            """

        ob = OrderBlock(base_candle=base_candle,
                        icl=initial_pivot_candle_liquidity,
                        ob_type="long" if base_pivot_type == "valley" else "short")

        # Try to find a valid exit candle for the order block.
        ob.register_exit_candle(self.pair_df, position_activation_threshold)

        # If a valid exit candle is found, form the reentry check window.
        if ob.price_exit_index is not None:
            # In validation mode, the algorithm won't avoid posting positions that have been entered by price movements after the
            # activation threshold, therefore the positions can be more thoroughly examined.
            if constants.validation_mode:
                reentry_check_window: pd.DataFrame = self.pair_df.iloc[ob.price_exit_index + 1:position_activation_threshold]
            else:
                reentry_check_window: pd.DataFrame = self.pair_df.iloc[ob.price_exit_index + 1:]

        # If no exit candle is found, that means that order block isn't valid. None is returned.
        else:
            return None

        # Order block condition checks
        ob.check_reentry_condition(reentry_check_window)

        if constants.validation_mode:
            conditions_check_window: pd.DataFrame = self.pair_df[ob.start_index:position_activation_threshold]
        else:
            conditions_check_window: pd.DataFrame = self.pair_df[ob.start_index:]

        ob.set_condition_check_window(conditions_check_window)
        ob.check_fvg_condition()
        ob.check_stop_break_condition()

        return ob

    @staticmethod
    def register_possible_position_entries(position: Position, latest_candle):
        """
        Sets the .has_been_entered property of all positions which have been entered by the latest candle.

        Args:
            position (Position): The position to check.
            latest_candle (pd.Series): The latest candle.
        """

        if position.type == 'long':
            if latest_candle.low <= position.entry_price:
                position.register_entered()

        elif latest_candle.high >= position.entry_price:
            position.register_entered()
