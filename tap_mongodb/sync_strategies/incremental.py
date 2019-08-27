#!/usr/bin/env python3
import time
from bson import objectid
import copy
import pymongo
import singer
from singer import metadata, metrics, utils
import tap_mongodb.sync_strategies.common as common

LOGGER = singer.get_logger()


def update_bookmark(row, state, tap_stream_id, replication_key_name):
    replication_key_value = row.get(replication_key_name)
    if replication_key_value:
        replication_key_type = replication_key_value.__class__.__name__

        replication_key_value_bookmark = common.class_to_string(replication_key_value,
                                                                replication_key_type)
        state = singer.write_bookmark(state,
                                      tap_stream_id,
                                      'replication_key_value',
                                      replication_key_value_bookmark)
        state = singer.write_bookmark(state,
                                      tap_stream_id,
                                      'replication_key_type',
                                      replication_key_type)    

def sync_collection(client, stream, state, projection):
    tap_stream_id = stream['tap_stream_id']
    LOGGER.info('Starting incremental sync for {}'.format(tap_stream_id))

    mdata = metadata.to_map(stream['metadata'])
    stream_metadata = mdata.get(())
    database_name = stream_metadata['database-name']

    db = client[database_name]
    collection = db[stream['stream']]

    #before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, stream['tap_stream_id'], 'version') is None

    #pick a new table version if last run wasn't interrupted
    if first_run:
        stream_version = int(time.time() * 1000)
    else:
        stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    activate_version_message = singer.ActivateVersionMessage(
        stream=common.calculate_destination_stream_name(stream),
        version=stream_version
    )


    # For the initial replication, emit an ACTIVATE_VERSION message
    # at the beginning so the records show up right away.
    if first_run:
        singer.write_message(activate_version_message)

    # get bookmarks if they exist
    stream_state = state.get('bookmarks', {}).get(tap_stream_id, {})
    replication_key_bookmark = stream_state.get('replication_key_name')
    replication_key_value_bookmark = stream_state.get('replication_key_value')
    replication_key_type_bookmark = stream_state.get('replication_key_type')


    if not replication_key_bookmark:
        replication_key_bookmark = stream_metadata.get('replication-key')
        state = singer.write_bookmark(state,
                                      tap_stream_id,
                                      'replication_key_name',
                                      replication_key_bookmark)

    find_filter = {}
    if replication_key_value_bookmark:
        find_filter[replication_key_bookmark] = {}
        find_filter[replication_key_bookmark]['$gte'] = common.string_to_class(replication_key_value_bookmark,
                                                                             replication_key_type_bookmark)


    query_message = 'Querying {} with:\n\tFind Parameters: {}'.format(
        stream['tap_stream_id'],
        find_filter)
    if projection:
        query_message += '\n\tProjection: {}'.format(projection)
    LOGGER.info(query_message)


    with collection.find(find_filter,
                         projection,
                         sort=[(replication_key_bookmark, pymongo.ASCENDING)]) as cursor:
        rows_saved = 0

        time_extracted = utils.now()

        start_time = time.time()

        for row in cursor:
            rows_saved += 1

            record_message = common.row_to_singer_record(stream,
                                                         row,
                                                         stream_version,
                                                         time_extracted)

            singer.write_message(record_message)

            update_bookmark(row, state, tap_stream_id, replication_key_bookmark)
            
            if rows_saved % common.UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))


        common.COUNTS[tap_stream_id] += rows_saved
        common.TIMES[tap_stream_id] += time.time()-start_time

    singer.write_message(activate_version_message)

    LOGGER.info('Syncd {} records for {}'.format(rows_saved, tap_stream_id))