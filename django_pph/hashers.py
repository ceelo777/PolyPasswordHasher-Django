from base64 import b64decode

from django.core.exceptions import ImproperlyConfigured
from django.contrib.auth.hashers import BasePasswordHasher, mask_hash
from django.utils.translation import ugettext_noop as _
from django.utils.crypto import pbkdf2

from django.contrib.auth.models import User

import hashlib
import logging
import datetime

try:
    from collections import OrderedDict
except ImportError:
    # Python<=2.6
    from django.utils.datastructures import SortedDict as OrderedDict
try:
    from Crypto.Cipher import AES
    from Crypto.Hash import SHA256
except ImportError:
    raise ImproperlyConfigured('You must have PyCrypto installed in order to use the PolyPasswordHasher')

from .shamirsecret import ShamirSecret
from .settings import SETTINGS
from .utils import (LockedException, b64enc, bin64enc, binary_type, get_cache,
                    constant_time_compare, do_bytearray_xor)

cache = get_cache('pph')
logger = logging.getLogger('django.security.PPH')

class PolyPasswordHasher(BasePasswordHasher):
    algorithm = 'pph'
    iterations = 12000
    threshold = SETTINGS['THRESHOLD']
    partialbytes = SETTINGS['PARTIALBYTES']

    data = {
        'is_unlocked': False,
        'secret': None,
        'nextavailableshare': 1,
        'shamirsecretobj': None,
        'thresholdlesskey': None,
        'last_unlocked' : datetime.datetime.utcnow(),
    }
    defaults = data.copy()

    def digest(self, password, salt, iterations):
        return pbkdf2(password, salt, iterations, digest=hashlib.sha256)

    def update(self, **attrs):
        self.data.update(attrs)
        cache.set('hasher', self.data)

    def load(self):
        self.data = cache.get('hasher') or self.defaults

    def encode(self, password, salt, iterations=None):
        if not self.data['is_unlocked']:
            self.load()


        assert salt is not None
        assert password is not None

        # we pre-parse the input string to verify which kind of entry this
        # belongs to
        if '$' in salt:
            sharenumber = self.data['nextavailableshare']
            self.data['nextavailableshare'] += 1
            self.update()
            salt = salt.strip('$')
        else:
            sharenumber = 0

        if iterations is None:
            iterations = self.iterations
        
        # in case we are locked, we can do a normal hashing procedure and then
        # expect to update the user after we recover the secret
        if not self.data['is_unlocked'] or \
                self.data['thresholdlesskey'] is None:
            passhash = self.digest(password, salt, iterations)
            passhash = b64enc(passhash)
            logger.debug("creating locked-account {}".format(passhash))
            return "%s$-%s$%d$%s$%s" % (self.algorithm, sharenumber, iterations,
                    salt, passhash)

        # create_account(password, salt)
        # shareN + ^ + salt = a share
        # shareN is from nextavailableshare
        # when running encode w/ ^, nextavailableshare += 1
        # iterations => pbkdf2
        # pbkdf2 is hash function

        # we verify whether the entry is to be a threshold or thresholdless
        # account.
        if sharenumber == 0 or sharenumber is None:
            passhash = self._encrypt_entry(password, salt)
        else:
            passhash = self._polyhash_entry(password, salt, sharenumber)

        return "%s$%d$%d$%s$%s" % (self.algorithm, sharenumber, iterations,
                                   salt, passhash)

    def verify(self, password, encoded):
        if not self.data['is_unlocked']:
            self.load()

        algorithm, sharenumber, iterations, salt, original_hash = \
                encoded.split('$', 4)

        assert algorithm == self.algorithm
        
        # check if this is a non pph-protected hash, and just do normal 
        # verification for it.
        if sharenumber.startswith('-'):
            passhash = self.digest(password, salt, iterations)
            passhash = b64enc(passhash)
            logger.debug("verifying a locked account {}".format(passhash))
            return constant_time_compare(passhash, original_hash)

        
        sharenumber = int(sharenumber)

        if self.data['secret'] is not None and \
                self.data['thresholdlesskey'] is not None:

            if sharenumber != 0:
                proposed_hash = self._polyhash_entry(password, salt,
                        sharenumber)

            else:
                proposed_hash = self._encrypt_entry(password, salt)

            # We will also check the partial verification to notify of possible
            # break-in attempts
            partial_result = self._partial_verify(password, salt, original_hash,
                iterations, sharenumber)
            result = constant_time_compare(original_hash, proposed_hash)
            
            if partial_result and not result:
                logger.error("Failed login with correct partial bytes. " + 
                            "Possible database leak detected! The offending " +
                            "Hash is: {}".format(original_hash))
            
            return constant_time_compare(original_hash, proposed_hash)

        else:
            # try to infer the share from the information given
            # TODO: this could be optimized by merging the functionality from
            # _get_share... with _partial_verify...
            if sharenumber != 0:
                share = self._get_share_from_hash(password, salt, original_hash,
                        iterations)

                # we check for conflicts before inserting this into our cache
                value = cache.get(sharenumber)
                if value is not None:
                    original_share = b64enc(value)

                    new_share = b64enc(share)
                    # if they are not the same
                    if not constant_time_compare(original_share, new_share):
                        raise Exception("Cached share does not match the new "
                                        " share value!")
                else:
                    # this is a new share, add it to the cache and recombine if
                    # possible
                    cache.set(sharenumber, share)
                    sharenumbers = cache.get("sharenumbers")

                    if not sharenumbers:
                        sharenumbers = set()

                    sharenumbers.add(sharenumber)
                    cache.set("sharenumbers", sharenumbers)

                    if len(sharenumbers) >= self.threshold:
                        self._recombine()

            # partial verification step, if we are locked, let's try to log the
            # user in
            if self.partialbytes > 0:
                result = self._partial_verify(password, salt, original_hash,
                        iterations, sharenumber)
                return result

        raise LockedException

    def safe_summary(self, encoded):
        algorithm, sharenumber, iterations, salt, hash = encoded.split('$', 4)
        assert algorithm == self.algorithm
        return OrderedDict([
            (_('algorithm'), algorithm),
            (_('sharenumber'), sharenumber),
            (_('iterations'), iterations),
            (_('salt'), mask_hash(salt)),
            (_('hash'), mask_hash(hash)),
        ])

    def must_update(self, encoded):
        algorithm, sharenumber, iterations, salt, hash = encoded.split('$', 4)
        return int(iterations) != self.iterations

    def _polyhash_entry(self, password, salt, sharenumber):
        """
        private helper that computes a polyhashed entry with a given
        sharenumber, password and salt. Used in hash creation and verification.
        """
        assert self.data['shamirsecretobj'] is not None

        saltedpasswordhash = self.digest(password, salt, self.iterations)
        shamirsecretdata = self.data['shamirsecretobj'].compute_share(
                sharenumber)[1]
        passhash = do_bytearray_xor(saltedpasswordhash, shamirsecretdata)
        passhash = bin64enc(passhash)
        passhash += b64enc(saltedpasswordhash[len(saltedpasswordhash)
                                              - self.partialbytes:])
        return passhash

    def _encrypt_entry(self, password, salt):

        assert self.data['thresholdlesskey'] is not None

        saltedpasswordhash = self.digest(password, salt, self.iterations)
        passhash = AES.new(self.data['thresholdlesskey']).encrypt(
                saltedpasswordhash)
        passhash = bin64enc(passhash)
        passhash += b64enc(saltedpasswordhash[len(saltedpasswordhash)
                                              - self.partialbytes:])
        return passhash

    def _partial_verify(self, password, salt, passhash, iterations, 
            sharenumber):

        saltedpasswordhash = b64enc(self.digest(password , salt, 
            iterations))
        partial_bytes = saltedpasswordhash[len(saltedpasswordhash)
                                           - self.partialbytes:]
        original_partial_bytes = passhash[len(passhash) - self.partialbytes:]
        result = constant_time_compare(partial_bytes, original_partial_bytes)

        if result:
            # we will populate a list with the hashes that have been
            # partially verificated. We will check this list for consistency
            # once the secret is recovered.
            partial_verificated_hashes = cache.get('partial_hashes')
            if partial_verificated_hashes is None:
                partial_verificated_hashes = {}

            if passhash not in partial_verificated_hashes:
                partial_verificated_hashes[passhash] = (sharenumber, 
                        saltedpasswordhash) 
            cache.set('partial_hashes', partial_verificated_hashes)

        return result

    # private helper to provide shares from hash ^ passhash
    def _get_share_from_hash(self, password, salt, passhash, iterations):

        passhash = binary_type(passhash)
        saltedpasswordhash = self.digest(password, salt, iterations)
        byte_passhash = b64decode(passhash[:len(passhash) - self.partialbytes])
        return do_bytearray_xor(byte_passhash, saltedpasswordhash)

    def verify_secret(self, secret):
        """
        Checks whether the secret given contains a
        proper fingerprint with the following form:

        [28 bytes random data][4 bytes hash of random data]

        the boolean returned indicates whether it falls under the
        fingerprint or not
        """
        secret = binary_type(secret)
        secret_length = SETTINGS['SECRET_LENGTH']
        verification_len = SETTINGS['SECRET_VERIFICATION_BYTES']
        random_data = secret[:secret_length - verification_len]
        secret_hash = self.digest(random_data, None, 1)
        secret_hash_text = b64enc(secret_hash)[:verification_len]
        return constant_time_compare(secret[secret_length - verification_len:],
                                     secret_hash_text)

    def update_hash_thresholdless(self, hash):
        """
        Attempt to encrypt an existing hash with the thresholdless key

        This expects the ascii-encoded version of the hash
        """

        byte_hash = b64decode(hash)
        passhash = AES.new(self.data['thresholdlesskey']).encrypt(byte_hash)
        passhash = bin64enc(passhash)
        passhash += b64enc(byte_hash[len(byte_hash) - self.partialbytes:])
        return passhash

    def update_hash_threshold(self, hash):
        """
        Attempt to produce a polyhashed entry with an already existing 
        hash string

        This expects the ascii-encoded version of the hash
        """

        byte_hash = b64decode(hash)

        sharenumber = self.data['nextavailableshare']
        self.data['nextavailableshare'] += 1
        self.update()

        shamirsecretdata = self.data['shamirsecretobj'].compute_share(
                sharenumber)[1]
        passhash = do_bytearray_xor(byte_hash, shamirsecretdata)
        passhash = bin64enc(passhash)
        passhash += b64enc(byte_hash[len(byte_hash) - self.partialbytes:])
        return passhash, sharenumber
 
    def _recombine(self):
        """
        Attempt to restore the secret when a threshold of shares has been met.
        """
        sharenumbers = cache.get("sharenumbers")
        assert sharenumbers is not None

        recombination_shares = []
        for share in sharenumbers:
            share_value = cache.get(share)
            assert share_value is not None
            current_share = (int(share), share_value)
            recombination_shares.append(current_share)

        self.data['shamirsecretobj'] = ShamirSecret(self.threshold)
        self.data['shamirsecretobj'].recover_secretdata(recombination_shares)
        self.data['secret'] = self.data['shamirsecretobj'].secretdata

        if not self.verify_secret(self.data['secret']):
            raise Exception("Couldn't recombine store!")

        self.data['thresholdlesskey'] = self.data['secret']
        self.data['is_unlocked']=1

        self._verify_previous_hashes()
        self._update_locked_hashes()

        self.data['last_unlocked'] = datetime.datetime.utcnow()
        self.update()

    def _verify_previous_hashes(self):
        partially_verified_hashes = cache.get('partial_hashes')

        if partially_verified_hashes is None:
            return

        for original_hash in partially_verified_hashes:
            sharenumber, saltedhash = partially_verified_hashes[original_hash]

            # We only verify thresholdless accounts because threshold accounts
            # would fail in the recombination phase. 
            if sharenumber == 0:
                byte_hash = b64decode(saltedhash)
                passhash = AES.new(
                        self.data['thresholdlesskey']).encrypt(byte_hash)
                passhash = bin64enc(passhash)
                hashlen = len(passhash)
                if not constant_time_compare(passhash, original_hash[:hashlen]):
                        logger.error("original hash mismatches partial " +
                        "verification! Possible break-in detected! The " +
                        "offending hash is {}".format(original_hash[:hashlen]))

    def _update_locked_hashes(self):
        all_users = User.objects.filter(
                date_joined__gte=self.data['last_unlocked'])

        assert self.data['is_unlocked'] == 1
        assert self.data['thresholdlesskey'] is not None
        assert self.data['secret'] is not None

        for user in all_users:
            algorithm, sharenumber, iterations, salt, original_hash = \
                    user.password.split("$",4)
            if sharenumber.startswith('-'):
                sharenumber.strip('-')
                sharenumber = int(sharenumber)
                if sharenumber == 0:
                    passhash = self.update_hash_thresholdless(original_hash)
                    password = "{s}${d}${s}${s}${s}".format(algorithm,
                            sharenumber, iterations, salt, passhash)
                    user.password = password
                else:
                    passhash, sharenumber= update_hash_threshold(original_hash)
                    password = "%s$%d$%s$%s$%s" % (algorithm, sharenumber, 
                            iterations, salt. passhash)
                    user.password = password

                user.save()

        return
