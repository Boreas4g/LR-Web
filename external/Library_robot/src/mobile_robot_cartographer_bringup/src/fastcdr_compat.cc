namespace eprosima
{
namespace fastcdr
{
class Cdr;
}  // namespace fastcdr
}  // namespace eprosima

extern "C" eprosima::fastcdr::Cdr& fastcdr_serialize_int(
    eprosima::fastcdr::Cdr* self,
    int value) asm("_ZN8eprosima7fastcdr3Cdr9serializeEi");

extern "C" eprosima::fastcdr::Cdr& fastcdr_serialize_uint(
    eprosima::fastcdr::Cdr* self,
    unsigned int value) asm("_ZN8eprosima7fastcdr3Cdr9serializeEj");

extern "C" eprosima::fastcdr::Cdr& fastcdr_serialize_uint(
    eprosima::fastcdr::Cdr* self,
    unsigned int value)
{
    return fastcdr_serialize_int(self, static_cast<int>(value));
}
