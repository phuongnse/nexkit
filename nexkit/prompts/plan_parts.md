
## Earlier parts this part relies on

This issue is a part of #$parent, split from it because it was too large for one session.
It needs these earlier parts first:

<untrusted>
$parts
</untrusted>

## How to rely on the earlier parts

- Before you plan on top of an earlier part, name what this part assumes it does, and
  check each assumption in the code: read the code that part added and follow the path
  this part needs end to end, such as the protocol, the address and the data a request
  passes through. Do not take the part's title, body or plan as proof.
- Write each assumption and how the code shows it in `decisions`: the assumption as the
  `decision`, the file and function that show it as the `reason`.
- When the code does not do what this part needs, or the part is still open, never plan
  as if it did. Say so in `risks`, and either plan the missing piece in this part, when it
  is small and belongs here, or ask in `questions`.
